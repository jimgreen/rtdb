"""本地电力 E 文件与 Excel (.xlsx / .xls) 工作会话管理器。

核心思路（Local File Session Bridge）：
1. 加载本地 E 文件（.e/.qs/.dt/.txt/.dat）或 Excel 工作簿（.xlsx/.xls，多 Sheet）；
2. 在独立隔离的临时目录中创建一个标准的多表 RTDB SQLite 会话库（efile_session.db），
   并构建独立的 SQLiteManager / SQLiteManagement / TaskRegistry / AuditDelivery / SampleTrendRegistry；
3. 前端与服务端只需透传 efile_session=<id>，即可 100% 复用现有全部 SQL 查询、
   行内编辑、批量修改、新增/删除记录、新建/改名/删除表（Sheet）与表结构编辑功能；
4. 保存/导出时，从会话库读取最新表结构与数据，无损回写为标准 E 文件、.xlsx 或 .xls。
"""
from __future__ import annotations

import base64
import csv
import datetime
import io
import math
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core import SQLiteDatabase, SchemaError, TableDefinition
from core.definition_files import decode_upload, infer_type, logical_lines, normalize_definition_labels, tokens
from core.sqlite_schema import load_embedded_database, write_embedded_definition
from .errors import APIError
from .management import SQLiteManagement
from .sample_points import SampleTrendRegistry
from .sqlite_service import SQLiteManager, atomic_json, python_definition
from .tasks import TaskRegistry
from .write_receipts import AuditDelivery
from .xls_codec import OLE2_SIGNATURE, ZIP_SIGNATURE, read_xls_sheets, write_xls_sheets


SESSION_DB_FILENAME = 'efile_session.db'
SESSION_DB_STEM = 'efile_session'
SYNTHETIC_PK_COLUMN = '_e_rowid'
MAX_LOCAL_FILE_BYTES = 24 * 1024 * 1024
MAX_TABLES_PER_FILE = 200

_TAG_OPEN_RE = re.compile(r'^<([^/\s>][^>]*)>$')
_TAG_CLOSE_RE = re.compile(r'^</\s*([^\s>]+)\s*>$')
_IDENT_CLEAN_RE = re.compile(r'[^A-Za-z0-9_]+')
_SHEET_INVALID_RE = re.compile(r'[:\\/?*\[\]]')
_RESERVED_NAMES = {'sqlite_master', 'sqlite_sequence', 'metadata', 'data'}
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _sanitize_identifier(raw: str, prefix: str, seen: set[str]) -> str:
    """将任意中英文名称清洗为合法且全局唯一的 SQLite / RTDB 标识符。"""
    text = str(raw or '').strip()
    cleaned = _IDENT_CLEAN_RE.sub('_', text).strip('_')
    if not cleaned:
        idx = 1
        while f'{prefix}_{idx}'.casefold() in seen:
            idx += 1
        candidate = f'{prefix}_{idx}'
        seen.add(candidate.casefold())
        return candidate
    if cleaned[0].isdigit():
        cleaned = f'{prefix}_{cleaned}'
    if cleaned.lower() in _RESERVED_NAMES or cleaned.lower().startswith(('sqlite_', 'rtdb_')):
        cleaned = f'{prefix}_{cleaned}'
    cleaned = cleaned[:56]
    candidate = cleaned
    suffix = 2
    while candidate.casefold() in seen:
        tail = f'_{suffix}'
        candidate = f'{cleaned[:56 - len(tail)]}{tail}'
        suffix += 1
    seen.add(candidate.casefold())
    return candidate


def _sanitize_sheet_name(raw: str, seen: set[str], fallback_index: int) -> str:
    """清洗为合法且不重复的 Excel Sheet 名称（最多 31 字符）。"""
    text = _SHEET_INVALID_RE.sub('_', str(raw or '').strip()).strip("'")
    if not text:
        text = f'Sheet{fallback_index}'
    text = text[:31]
    candidate = text
    suffix = 2
    while candidate.casefold() in seen:
        tail = f'_{suffix}'
        candidate = f'{text[:31 - len(tail)]}{tail}'
        suffix += 1
    seen.add(candidate.casefold())
    return candidate


def _detect_file_kind(filename: str, raw_bytes: bytes | None, explicit_kind: str | None = None) -> str:
    """根据显式参数、扩展名与文件魔数识别文件类型：e / xlsx / xls / csv。"""
    if explicit_kind in ('e', 'xlsx', 'xls', 'csv'):
        return explicit_kind
    lower = (filename or '').strip().lower()
    if lower.endswith(('.xlsx', '.xlsm')):
        return 'xlsx'
    if lower.endswith('.xls'):
        return 'xls'
    if lower.endswith(('.csv', '.tsv')):
        return 'csv'
    if raw_bytes:
        if raw_bytes[:4] == ZIP_SIGNATURE:
            return 'xlsx'
        if raw_bytes[:8] == OLE2_SIGNATURE:
            return 'xls'
    return 'e'


def _detect_csv_delimiter(text: str, filename: str) -> str:
    """探测 CSV / TSV 分隔符（优先 .tsv -> Tab，否则使用 csv.Sniffer 并兜底逗号）。"""
    if (filename or '').lower().endswith('.tsv'):
        return '\t'
    sample_lines = [ln for ln in text.splitlines() if ln.strip()][:20]
    sample = '\n'.join(sample_lines)
    if sample:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',\t;|')
            if dialect.delimiter in (',', '\t', ';', '|'):
                return dialect.delimiter
        except csv.Error:
            pass
    # 轻量兜底：统计首行常见分隔符频次
    first = sample_lines[0] if sample_lines else ''
    counts = {d: first.count(d) for d in (',', '\t', ';')}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ','


def _parse_csv_document(text: str, filename: str) -> dict:
    """解析本地 CSV / TSV 文件，返回表名、分隔符、换行符与规范化行列数据。"""
    newline = '\r\n' if '\r\n' in text else '\n'
    delimiter = _detect_csv_delimiter(text, filename)
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter)
    raw_rows = [row for row in reader]

    def is_blank(row: list[str]) -> bool:
        return not any(cell.strip() != '' for cell in row)

    while raw_rows and is_blank(raw_rows[-1]):
        raw_rows.pop()
    first = 0
    while first < len(raw_rows) and is_blank(raw_rows[first]):
        first += 1
    active = raw_rows[first:]
    stem = Path(filename or 'data.csv').stem or 'data'

    if not active:
        return {
            'table_label': stem,
            'delimiter': delimiter,
            'newline': newline,
            'raw_columns': [{'original_name': 'col_1', 'chn_name': '列1'}],
            'raw_rows': [],
        }

    max_col = max((len(r) for r in active), default=1)
    header = active[0]
    raw_columns: list[dict] = []
    for c_idx in range(max_col):
        raw_head = header[c_idx].strip() if c_idx < len(header) else ''
        head_text = raw_head or f'列{c_idx + 1}'
        raw_columns.append({'original_name': head_text, 'chn_name': head_text})

    data_rows: list[list] = []
    for row in active[1:]:
        if is_blank(row):
            continue
        padded = []
        for c_idx in range(max_col):
            val = row[c_idx] if c_idx < len(row) else ''
            padded.append(None if val == '' else val)
        data_rows.append(padded)

    return {
        'table_label': stem,
        'delimiter': delimiter,
        'newline': newline,
        'raw_columns': raw_columns,
        'raw_rows': data_rows,
    }


def _has_leading_zero_code(text: str) -> bool:
    """检测形如 001 / 0102 的编号字符串，避免转整型丢失前导零。"""
    s = text.strip()
    if s.startswith(('+', '-')):
        s = s[1:]
    return len(s) > 1 and s[0] == '0' and s[1].isdigit()


def _normalize_excel_cell(value):
    """将 openpyxl / xls 单元格值标准化为 RTDB 友好的基础 Python 值。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if _INT64_MIN <= value <= _INT64_MAX:
            return value
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        rounded = round(value)
        if abs(value - rounded) <= 1e-9 and abs(rounded) <= (1 << 53):
            return int(rounded)
        return value
    if isinstance(value, datetime.datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0 and value.microsecond == 0:
            return value.strftime('%Y-%m-%d')
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, datetime.date):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, datetime.time):
        return value.strftime('%H:%M:%S')
    return str(value)


def _infer_column_type(values: list) -> str:
    """稳健推断单列数据类型，并对前导零编号与超界整数自动保留为 str。"""
    non_null = [v for v in values if v is not None and not (isinstance(v, str) and v.strip() == '')]
    if not non_null:
        return 'str'

    # 如果全部已经是原生 Python 数值/布尔（Excel 场景）
    if all(isinstance(v, bool) for v in non_null):
        return 'bool'
    if all(isinstance(v, int) and not isinstance(v, bool) and _INT64_MIN <= v <= _INT64_MAX for v in non_null):
        return 'int'
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) for v in non_null):
        return 'float'

    # 文本形式推断（E 文件或混合文本 Excel 列）
    str_vals = [str(v) for v in non_null]
    if any(_has_leading_zero_code(s) for s in str_vals):
        return 'str'
    inferred = infer_type(str_vals)
    if inferred == 'int':
        for s in str_vals:
            try:
                num = int(s.strip())
                if not (_INT64_MIN <= num <= _INT64_MAX):
                    return 'str'
            except ValueError:
                return 'str'
    return inferred


def _coerce_value(value, data_type: str):
    """按目标列类型安全转换单格值，失败时抛出 ValueError 触发整列降级为 str。"""
    if value is None:
        return None
    if data_type == 'str':
        return value if isinstance(value, str) else str(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == '':
            return None
        if data_type == 'bool':
            low = stripped.lower()
            if low in ('true', '1', 'yes', 'y'):
                return True
            if low in ('false', '0', 'no', 'n'):
                return False
            raise ValueError(f'无法转为布尔值: {value!r}')
        if data_type == 'int':
            num = int(stripped)
            if not (_INT64_MIN <= num <= _INT64_MAX):
                raise ValueError('整数超出 64 位范围')
            return num
        if data_type == 'float':
            num = float(stripped)
            if not math.isfinite(num):
                raise ValueError('非有限浮点数')
            return num
        return value
    if data_type == 'bool':
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError('无法转为布尔值')
    if data_type == 'int':
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            if not (_INT64_MIN <= value <= _INT64_MAX):
                raise ValueError('整数超出 64 位范围')
            return value
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            num = int(value)
            if _INT64_MIN <= num <= _INT64_MAX:
                return num
        raise ValueError('无法转为整数')
    if data_type == 'float':
        if isinstance(value, bool):
            return float(int(value))
        num = float(value)
        if not math.isfinite(num):
            raise ValueError('非有限浮点数')
        return num
    return str(value)


def _pick_primary_key(col_names: list[str], col_types: dict[str, str], rows: list[dict]) -> str | None:
    """选择可直接充当唯一非空主键的现有列；若无合适列则返回 None（自动注入 _e_rowid）。"""
    if not rows or not col_names:
        return None
    preferred = [c for c in col_names if c.lower() in ('id', 'key', 'idx')]
    candidates = preferred + [c for c in col_names if c not in preferred][:1]
    for col in candidates:
        if col_types.get(col) not in ('int', 'str'):
            continue
        seen = set()
        valid = True
        for row in rows:
            val = row.get(col)
            if val is None or (isinstance(val, str) and val == ''):
                valid = False
                break
            if val in seen:
                valid = False
                break
            seen.add(val)
        if valid:
            return col
    return None


def _disp_type_for(kind: str) -> str:
    return {'int': '整数', 'float': '浮点数', 'bool': '布尔'}.get(kind, '文本')


def _build_table_bundle(
    *,
    eng_name: str,
    chn_name: str,
    raw_columns: list[dict],
    raw_rows: list[list],
) -> tuple[dict, list[dict], bool, dict[str, str]]:
    """将原始列与二维行数据转换为 RTDB TableDefinition 字典、行列表与元信息。

    raw_columns 每项为 {'original_name': str, 'chn_name': str}。
    返回 (definition_dict, converted_rows, synthetic_pk, col_original_names)。
    """
    if not raw_columns:
        raw_columns = [{'original_name': 'col_1', 'chn_name': '列1'}]

    seen_cols: set[str] = {SYNTHETIC_PK_COLUMN.casefold()}
    col_specs: list[dict] = []
    col_original_names: dict[str, str] = {}
    for idx, col in enumerate(raw_columns, 1):
        orig = str(col.get('original_name') or '').strip()
        chn = str(col.get('chn_name') or '').strip()
        eng = _sanitize_identifier(orig or f'col_{idx}', 'col', seen_cols)
        col_specs.append({
            'eng_name': eng,
            'chn_name': chn or orig or eng,
            'original_name': orig or eng,
        })
        col_original_names[eng] = orig or eng

    col_count = len(col_specs)
    # 按列推断类型并执行容错转换（单列任意值无法转换则整列降级为 str）
    col_types: dict[str, str] = {}
    converted_columns: dict[str, list] = {}
    for c_idx, spec in enumerate(col_specs):
        col_vals = [row[c_idx] if c_idx < len(row) else None for row in raw_rows]
        inferred = _infer_column_type(col_vals)
        try:
            converted = [_coerce_value(v, inferred) for v in col_vals]
        except ValueError:
            inferred = 'str'
            converted = [_coerce_value(v, 'str') for v in col_vals]
        col_types[spec['eng_name']] = inferred
        converted_columns[spec['eng_name']] = converted

    row_dicts: list[dict] = []
    for r_idx in range(len(raw_rows)):
        row_dicts.append({spec['eng_name']: converted_columns[spec['eng_name']][r_idx] for spec in col_specs})

    pk_col = _pick_primary_key([s['eng_name'] for s in col_specs], col_types, row_dicts)
    synthetic_pk = pk_col is None

    columns_def: list[dict] = []
    if synthetic_pk:
        columns_def.append({
            'eng_name': SYNTHETIC_PK_COLUMN,
            'chn_name': '行号',
            'data_type': 'int',
            'disp_type': '整数',
            'primary_key': True,
        })
        for idx, row in enumerate(row_dicts, 1):
            row[SYNTHETIC_PK_COLUMN] = idx

    for spec in col_specs:
        eng = spec['eng_name']
        kind = col_types[eng]
        columns_def.append({
            'eng_name': eng,
            'chn_name': spec['chn_name'],
            'data_type': kind,
            'disp_type': _disp_type_for(kind),
            'primary_key': (eng == pk_col),
        })

    definition = normalize_definition_labels({
        'head': {
            'eng_name': eng_name,
            'chn_name': chn_name or eng_name,
        },
        'columns': columns_def,
    })
    return definition, row_dicts, synthetic_pk, col_original_names


# ---------------------------------------------------------------------------
# E 文件解析与无损序列化
# ---------------------------------------------------------------------------

def parse_efile_document(text: str) -> dict:
    """完整解析标准电力 E 文件，保留头部、原始标签属性、中文描述与数据行。"""
    newline = '\r\n' if '\r\n' in text else '\n'
    header_lines: list[str] = []
    modules: list[dict] = []
    current: dict | None = None

    for line_no, line in logical_lines(text):
        if not line:
            continue
        if current is None:
            if line.startswith(('<!', '//')):
                header_lines.append(line)
                continue
            open_match = _TAG_OPEN_RE.match(line)
            if not open_match:
                header_lines.append(line)
                continue
            inner = open_match.group(1).strip()
            parts = inner.split(None, 1)
            full_tag = parts[0]
            props = parts[1] if len(parts) > 1 else ''
            if '::' in full_tag:
                raw_name, tag = full_tag.split('::', 1)
            else:
                raw_name, tag = full_tag, ''
            current = {
                'raw_name': raw_name.strip(),
                'tag': tag.strip(),
                'full_tag': full_tag.strip(),
                'props': props,
                'open_line': line,
                'close_line': f'</{full_tag.strip()}>',
                'columns': [],
                'descriptions': [],
                'has_desc_line': False,
                'rows': [],
                'extra_comments': [],
                'line_no': line_no,
            }
            continue

        close_match = _TAG_CLOSE_RE.match(line)
        if close_match:
            closing_tag = close_match.group(1).strip()
            if closing_tag.casefold() in (current['full_tag'].casefold(), current['raw_name'].casefold()):
                current['close_line'] = line
                modules.append(current)
                current = None
                continue
            raise SchemaError(f'第 {line_no} 行：结束标签 </{closing_tag}> 与开始标签 <{current["full_tag"]}> 不匹配')

        if line.startswith('<!'):
            current['extra_comments'].append(line)
            continue

        if line.startswith('@') and not current['columns']:
            col_tokens = tokens(line[1:])
            if not col_tokens:
                raise SchemaError(f'第 {line_no} 行：@ 列名行为空')
            current['columns'] = col_tokens
            continue

        if line.startswith('//'):
            if current['columns'] and not current['has_desc_line'] and not current['rows']:
                desc_tokens = tokens(line[2:])
                if desc_tokens:
                    current['descriptions'] = desc_tokens
                    current['has_desc_line'] = True
                    continue
            current['extra_comments'].append(line)
            continue

        if line.startswith('#'):
            if not current['columns']:
                raise SchemaError(f'第 {line_no} 行：数据行出现在 @ 列定义之前')
            body = line[1:]
            row_tokens = tokens(body)
            # 某些 E 文件以 "# 1 ..." 形式书写且带行号或直接跟字段值
            expected = len(current['columns'])
            if len(row_tokens) > expected:
                # 若列定义未含序号列而数据行多出 1 列且像首列序号，则仍按截断/扩列保底
                # 优先扩展未知列，绝不静默丢数据
                extra_count = len(row_tokens) - expected
                for extra_i in range(extra_count):
                    current['columns'].append(f'extra_{expected + extra_i + 1}')
            values = [None if tok == r'\N' else tok for tok in row_tokens]
            if len(values) < len(current['columns']):
                values.extend([None] * (len(current['columns']) - len(values)))
            current['rows'].append(values)
            continue

    if current is not None:
        raise SchemaError(f'第 {current["line_no"]} 行：模块 <{current["full_tag"]}> 缺少结束标签')
    if not modules:
        raise SchemaError('未在文件中解析到任何 E 文件模块（<TableName> ... </TableName>）')
    if len(modules) > MAX_TABLES_PER_FILE:
        raise SchemaError(f'单个文件最多支持 {MAX_TABLES_PER_FILE} 张表，当前包含 {len(modules)} 张')

    return {
        'newline': newline,
        'header_lines': header_lines,
        'modules': modules,
    }


def _format_efile_token(value) -> str:
    """将单格值格式化为符合电力 E 语言规范的词元。"""
    if value is None:
        return r'\N'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return r'\N'
        return repr(value) if not value.is_integer() else str(value)
    text = str(value)
    if text == '':
        return '""'
    needs_quote = (
        text == r'\N'
        or any(ch.isspace() or ch in "\"'" for ch in text)
        or text.startswith(('#', '@', '//', '<!', '<'))
    )
    if needs_quote:
        return '"' + text.replace('"', '""') + '"'
    return text


def _format_efile_comment_token(text: str) -> str:
    clean = str(text or '').strip()
    if not clean:
        return '-'
    if any(ch.isspace() or ch in "\"'" for ch in clean):
        return '"' + clean.replace('"', '""') + '"'
    return clean


# ---------------------------------------------------------------------------
# Excel (.xlsx / .xls) 解析与写出
# ---------------------------------------------------------------------------

def _parse_xlsx_sheets(raw_bytes: bytes) -> list[dict]:
    """使用 openpyxl 读取 .xlsx 全部工作表。"""
    try:
        import openpyxl
    except ImportError as error:
        raise APIError(500, '服务端未安装 openpyxl，无法解析 .xlsx 文件') from error
    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
    try:
        sheets: list[dict] = []
        for title in wb.sheetnames:
            ws = wb[title]
            rows = [[_normalize_excel_cell(cell) for cell in row] for row in ws.iter_rows(values_only=True)]
            sheets.append({'name': title, 'rows': rows})
        return sheets
    finally:
        wb.close()


def _parse_excel_sheets_to_modules(sheets: list[dict]) -> list[dict]:
    """将 Excel 多 Sheet 二维数组统一整理为表构建输入。"""
    if not sheets:
        raise SchemaError('Excel 工作簿中没有工作表')
    if len(sheets) > MAX_TABLES_PER_FILE:
        raise SchemaError(f'单个工作簿最多支持 {MAX_TABLES_PER_FILE} 个 Sheet，当前包含 {len(sheets)} 个')

    result: list[dict] = []
    for idx, sheet in enumerate(sheets, 1):
        sheet_name = str(sheet.get('name') or f'Sheet{idx}').strip() or f'Sheet{idx}'
        raw_rows = list(sheet.get('rows') or [])

        # 裁剪完全为空的尾行与前导空行
        def is_blank_row(row) -> bool:
            return not any(v is not None and str(v).strip() != '' for v in row)

        while raw_rows and is_blank_row(raw_rows[-1]):
            raw_rows.pop()
        first_non_blank = 0
        while first_non_blank < len(raw_rows) and is_blank_row(raw_rows[first_non_blank]):
            first_non_blank += 1
        active_rows = raw_rows[first_non_blank:]

        if not active_rows:
            # 空白 Sheet：保留可编辑单列空表
            result.append({
                'sheet_name': sheet_name,
                'raw_columns': [{'original_name': 'col_1', 'chn_name': '列1'}],
                'raw_rows': [],
            })
            continue

        # 计算有效最大列宽（裁掉尾部全空列）
        max_col = 0
        for row in active_rows:
            for c_idx in range(len(row) - 1, -1, -1):
                val = row[c_idx]
                if val is not None and str(val).strip() != '':
                    max_col = max(max_col, c_idx + 1)
                    break
        if max_col == 0:
            max_col = 1

        header_row = active_rows[0]
        data_rows = active_rows[1:]
        raw_columns: list[dict] = []
        for c_idx in range(max_col):
            raw_head = header_row[c_idx] if c_idx < len(header_row) else None
            head_text = '' if raw_head is None else str(raw_head).strip()
            if not head_text:
                head_text = f'列{c_idx + 1}'
            raw_columns.append({'original_name': head_text, 'chn_name': head_text})

        normalized_rows: list[list] = []
        for row in data_rows:
            if is_blank_row(row):
                continue
            padded = [_normalize_excel_cell(row[c] if c < len(row) else None) for c in range(max_col)]
            normalized_rows.append(padded)

        result.append({
            'sheet_name': sheet_name,
            'raw_columns': raw_columns,
            'raw_rows': normalized_rows,
        })
    return result


def _write_xlsx_sheets(sheets: list[dict]) -> bytes:
    """将多 Sheet 数据写出为标准 .xlsx 字节流（表头加粗、冻结首行、自适应列宽）。"""
    try:
        import openpyxl
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError as error:
        raise APIError(500, '服务端未安装 openpyxl，无法写出 .xlsx 文件') from error

    wb = openpyxl.Workbook()
    default_ws = wb.active
    wb.remove(default_ws)

    if not sheets:
        sheets = [{'name': 'Sheet1', 'rows': []}]

    header_font = Font(bold=True)
    for item in sheets:
        ws = wb.create_sheet(title=item['name'])
        rows = item.get('rows') or []
        col_widths: dict[int, int] = {}
        for r_idx, row in enumerate(rows, 1):
            ws.append(list(row))
            for c_idx, val in enumerate(row, 1):
                if r_idx == 1:
                    ws.cell(row=1, column=c_idx).font = header_font
                if val is not None:
                    text = str(val)
                    # 中文字符按双宽估算，限制单列最宽 48 字符
                    display_len = sum(2 if ord(ch) > 127 else 1 for ch in text[:60])
                    col_widths[c_idx] = min(48, max(col_widths.get(c_idx, 8), display_len + 3))
        if rows:
            ws.freeze_panes = 'A2'
        for c_idx, width in col_widths.items():
            ws.column_dimensions[get_column_letter(c_idx)].width = width

    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 会话上下文与生命周期管理
# ---------------------------------------------------------------------------

@dataclass
class LocalFileSession:
    """单个本地 E / Excel / CSV 文件的独立运行上下文。"""
    id: str
    filename: str
    kind: str  # 'e' | 'xlsx' | 'xls' | 'csv'
    encoding: str
    root_dir: Path
    data_dir: Path
    schema_dir: Path
    sqlite_manager: SQLiteManager
    management: SQLiteManagement
    tasks: TaskRegistry
    audit_delivery: AuditDelivery
    sample_trends: SampleTrendRegistry
    table_order: list[str] = field(default_factory=list)
    table_meta: dict[str, dict] = field(default_factory=dict)
    efile_header: list[str] = field(default_factory=list)
    efile_newline: str = '\n'
    csv_delimiter: str = ','
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)
    _is_efile_session: bool = True

    def summary(self) -> dict:
        listing = self.sqlite_manager.tables()
        tables = listing.get('tables', []) if isinstance(listing, dict) else []
        return {
            'id': self.id,
            'filename': self.filename,
            'kind': self.kind,
            'encoding': self.encoding,
            'database': SESSION_DB_FILENAME,
            'table_count': len(tables),
            'tables': [
                {
                    'name': item['name'],
                    'chn_name': (item.get('definition') or {}).get('head', {}).get('chn_name') or item['name'],
                    'row_count': item.get('row_count', 0),
                }
                for item in tables
            ],
            'created_at': self.created_at,
        }

    def close(self):
        for attr in ('sample_trends', 'tasks', 'audit_delivery', 'sqlite_manager'):
            obj = getattr(self, attr, None)
            if obj is not None and hasattr(obj, 'close'):
                try:
                    obj.close()
                except Exception:
                    pass
        shutil.rmtree(self.root_dir, ignore_errors=True)


class EFileSessionStore:
    """线程安全的本地 E / Excel 文件会话池，自动管控数量上限与目录回收。"""

    def __init__(self, base_dir: Path, *, max_sessions: int = 16):
        self.base_dir = Path(base_dir).resolve()
        self.max_sessions = max_sessions
        self._lock = threading.RLock()
        self._sessions: dict[str, LocalFileSession] = {}

    def list(self) -> list[dict]:
        with self._lock:
            return [sess.summary() for sess in sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)]

    def get(self, session_id: str) -> LocalFileSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise APIError(404, '本地文件会话不存在或已关闭，请重新打开文件')
            session.last_access = time.time()
            return session

    def close_session(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.close()
        return True

    def close_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
        if self.base_dir.is_dir():
            shutil.rmtree(self.base_dir, ignore_errors=True)

    def _evict_if_needed(self):
        while len(self._sessions) >= self.max_sessions:
            oldest_id = min(self._sessions, key=lambda sid: self._sessions[sid].last_access)
            victim = self._sessions.pop(oldest_id)
            victim.close()

    def create(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise APIError(400, '请求体必须是 JSON 对象')
        raw_filename = str(payload.get('filename') or 'untitled.e').strip()
        filename = Path(raw_filename).name or 'untitled.e'

        raw_bytes: bytes | None = None
        if isinstance(payload.get('content'), str) and payload.get('content'):
            try:
                raw_bytes = base64.b64decode(payload['content'], validate=True)
            except (ValueError, TypeError) as error:
                raise APIError(400, '文件内容不是有效的 Base64') from error
            if not raw_bytes or len(raw_bytes) > MAX_LOCAL_FILE_BYTES:
                raise APIError(413, '请选择不超过 24 MiB 的非空文件')

        kind = _detect_file_kind(filename, raw_bytes, payload.get('kind'))

        table_defs: list[dict] = []
        table_rows: list[tuple[str, list[dict]]] = []
        table_order: list[str] = []
        table_meta: dict[str, dict] = {}
        efile_header: list[str] = []
        efile_newline = '\n'
        csv_delimiter = ','
        detected_encoding = 'binary'

        seen_tables: set[str] = set()

        if kind == 'e':
            text, detected_encoding = decode_upload(payload)
            if detected_encoding == 'text':
                detected_encoding = 'utf-8-sig'
            parsed = parse_efile_document(text)
            efile_header = parsed['header_lines']
            efile_newline = parsed['newline']
            for idx, mod in enumerate(parsed['modules'], 1):
                base_name = mod['raw_name']
                if mod['tag'] and base_name.casefold() in seen_tables:
                    base_name = f"{mod['raw_name']}_{mod['tag']}"
                eng_name = _sanitize_identifier(base_name, 'table', seen_tables)
                chn_name = mod['full_tag'] if mod['tag'] else mod['raw_name']
                raw_cols = []
                for c_idx, col_name in enumerate(mod['columns']):
                    desc = mod['descriptions'][c_idx] if c_idx < len(mod['descriptions']) else ''
                    raw_cols.append({'original_name': col_name, 'chn_name': desc or col_name})
                definition, rows, synthetic_pk, col_orig = _build_table_bundle(
                    eng_name=eng_name,
                    chn_name=chn_name,
                    raw_columns=raw_cols,
                    raw_rows=mod['rows'],
                )
                table_defs.append(definition)
                table_rows.append((eng_name, rows))
                table_order.append(eng_name)
                table_meta[eng_name] = {
                    'raw_name': mod['raw_name'],
                    'tag': mod['tag'],
                    'full_tag': mod['full_tag'],
                    'props': mod['props'],
                    'open_line': mod['open_line'],
                    'close_line': mod['close_line'],
                    'has_desc_line': mod['has_desc_line'],
                    'extra_comments': mod['extra_comments'],
                    'synthetic_pk': synthetic_pk,
                    'col_original_names': col_orig,
                }
        elif kind == 'csv':
            text, detected_encoding = decode_upload(payload)
            if detected_encoding == 'text':
                detected_encoding = 'utf-8-sig'
            parsed_csv = _parse_csv_document(text, filename)
            efile_newline = parsed_csv['newline']
            csv_delimiter = parsed_csv['delimiter']
            label = parsed_csv['table_label']
            eng_name = _sanitize_identifier(label, 'csv_table', seen_tables)
            definition, rows, synthetic_pk, col_orig = _build_table_bundle(
                eng_name=eng_name,
                chn_name=label,
                raw_columns=parsed_csv['raw_columns'],
                raw_rows=parsed_csv['raw_rows'],
            )
            table_defs.append(definition)
            table_rows.append((eng_name, rows))
            table_order.append(eng_name)
            table_meta[eng_name] = {
                'sheet_name': label,
                'synthetic_pk': synthetic_pk,
                'col_original_names': col_orig,
            }
        else:
            if raw_bytes is None:
                raise APIError(400, 'Excel 文件必须通过 Base64 content 上传')
            try:
                sheets = _parse_xlsx_sheets(raw_bytes) if kind == 'xlsx' else read_xls_sheets(raw_bytes)
            except APIError:
                raise
            except Exception as error:
                raise APIError(400, f'解析 {kind.upper()} 文件失败：{error}') from error
            modules = _parse_excel_sheets_to_modules(sheets)
            detected_encoding = kind.upper()
            for idx, mod in enumerate(modules, 1):
                sheet_name = mod['sheet_name']
                eng_name = _sanitize_identifier(sheet_name, 'sheet', seen_tables)
                definition, rows, synthetic_pk, col_orig = _build_table_bundle(
                    eng_name=eng_name,
                    chn_name=sheet_name,
                    raw_columns=mod['raw_columns'],
                    raw_rows=mod['raw_rows'],
                )
                table_defs.append(definition)
                table_rows.append((eng_name, rows))
                table_order.append(eng_name)
                table_meta[eng_name] = {
                    'sheet_name': sheet_name,
                    'synthetic_pk': synthetic_pk,
                    'col_original_names': col_orig,
                }

        # 创建隔离临时目录与标准多表 RTDB SQLite 数据库
        session_id = uuid.uuid4().hex[:16]
        root_dir = self.base_dir / session_id
        data_dir = root_dir / 'data'
        schema_dir = root_dir / 'def'
        data_dir.mkdir(parents=True, exist_ok=True)
        schema_dir.mkdir(parents=True, exist_ok=True)

        try:
            document = {
                'database': {'eng_name': SESSION_DB_STEM, 'chn_name': filename},
                'tables': table_defs,
            }
            atomic_json(schema_dir / f'{SESSION_DB_STEM}.json', document)
            (schema_dir / f'{SESSION_DB_STEM}.py').write_text(python_definition(document), encoding='utf-8')

            with SQLiteDatabase(data_dir, schema_directory=schema_dir, database_file=SESSION_DB_FILENAME) as db:
                for eng_name, rows in table_rows:
                    table = db[eng_name]
                    if rows:
                        table.insert_many(rows)
                # 确保哪怕只有单张表，也保持多表分组布局，便于用户直接新增 Sheet / 表
                if len(table_defs) == 1:
                    self._ensure_grouped_layout(data_dir / SESSION_DB_FILENAME, table_defs[0], filename)

            root_manager = SQLiteManager(data_dir, schema_dir)
            # 本地临时文件会话允许用户在二次确认后直接删除非空 Sheet / 表
            root_manager._allow_drop_nonempty = True
            manager = root_manager.for_database(SESSION_DB_FILENAME)
            management = SQLiteManagement(manager)
            tasks = TaskRegistry(data_dir / '.web-tasks', root_manager)
            audit_delivery = AuditDelivery(root_manager)
            root_manager.audit_delivery = audit_delivery
            manager.audit_delivery = audit_delivery
            sample_trends = SampleTrendRegistry(data_dir)

            session = LocalFileSession(
                id=session_id,
                filename=filename,
                kind=kind,
                encoding=detected_encoding,
                root_dir=root_dir,
                data_dir=data_dir,
                schema_dir=schema_dir,
                sqlite_manager=manager,
                management=management,
                tasks=tasks,
                audit_delivery=audit_delivery,
                sample_trends=sample_trends,
                table_order=table_order,
                table_meta=table_meta,
                efile_header=efile_header,
                efile_newline=efile_newline,
                csv_delimiter=csv_delimiter,
            )
            with self._lock:
                self._evict_if_needed()
                self._sessions[session_id] = session
            return session.summary()
        except BaseException:
            shutil.rmtree(root_dir, ignore_errors=True)
            raise

    @staticmethod
    def _ensure_grouped_layout(db_path: Path, single_def: dict, db_label: str):
        """确保单表会话库也使用 grouped 物理表名，支持后续随时新增表/Sheet。"""
        import sqlite3
        from contextlib import closing

        embedded = load_embedded_database(db_path)
        if embedded and embedded.is_grouped:
            return
        name = single_def['head']['eng_name']
        with closing(sqlite3.connect(db_path, timeout=10, isolation_level=None)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                has_data = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='data'").fetchone()
                if has_data:
                    conn.execute(f'ALTER TABLE data RENAME TO "rtdb_data_{name}"')
                has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
                if has_meta:
                    conn.execute(f'ALTER TABLE metadata RENAME TO "rtdb_metadata_{name}"')
                desired = TableDefinition.from_mapping(single_def)
                write_embedded_definition(conn, desired, SESSION_DB_STEM, db_label)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _collect_latest_tables(self, session: LocalFileSession) -> list[dict]:
        """从会话 SQLite 库按原始顺序 + 新增表顺序读取最新表定义与全部行。"""
        db_path = session.data_dir / SESSION_DB_FILENAME
        embedded = load_embedded_database(db_path)
        if embedded is None:
            return []

        by_name = {tbl.eng_name: tbl for tbl in embedded.tables}
        ordered_names: list[str] = [n for n in session.table_order if n in by_name]
        for tbl in embedded.tables:
            if tbl.eng_name not in ordered_names:
                ordered_names.append(tbl.eng_name)

        collected: list[dict] = []
        with SQLiteDatabase(session.data_dir, schema_directory=session.schema_dir, database_file=SESSION_DB_FILENAME) as db:
            for name in ordered_names:
                table_def = by_name[name]
                table = db[name]
                rows = table.all()
                collected.append({
                    'name': name,
                    'definition': table_def.to_mapping(),
                    'rows': rows,
                    'meta': session.table_meta.get(name, {}),
                })
        return collected

    def export_file(self, session_id: str, *, target_encoding: str | None = None, target_kind: str | None = None, target_table: str | None = None) -> dict:
        """将会话当前所有表无损序列化为目标格式（E 文件 / .xlsx / .xls / .csv）。"""
        session = self.get(session_id)
        tables = self._collect_latest_tables(session)
        kind = target_kind if target_kind in ('e', 'xlsx', 'xls', 'csv') else session.kind

        if kind == 'e':
            text = self._serialize_efile(session, tables)
            enc = target_encoding or session.encoding or 'utf-8-sig'
            if enc not in ('utf-8', 'utf-8-sig', 'gb18030', 'utf-16'):
                enc = 'utf-8-sig'
            try:
                raw_bytes = text.encode(enc)
            except UnicodeEncodeError:
                enc = 'utf-8-sig'
                raw_bytes = text.encode(enc)
            return {
                'filename': session.filename,
                'kind': 'e',
                'encoding': enc,
                'media_type': 'text/plain; charset=utf-8',
                'bytes': raw_bytes,
                'text': text,
            }

        if kind == 'csv':
            chosen = None
            if target_table:
                chosen = next((item for item in tables if item['name'] == target_table), None)
            if chosen is None and tables:
                chosen = tables[0]
            buf = io.StringIO()
            writer = csv.writer(
                buf,
                delimiter=session.csv_delimiter or ',',
                lineterminator=session.efile_newline or '\r\n',
            )
            if chosen is not None:
                columns = chosen['definition']['columns']
                skip_synthetic = bool(chosen['meta'].get('synthetic_pk'))
                export_cols = [c for c in columns if not (skip_synthetic and c['eng_name'] == SYNTHETIC_PK_COLUMN)]
                if not export_cols:
                    export_cols = list(columns)
                writer.writerow([(c.get('chn_name') or '').strip() or c['eng_name'] for c in export_cols])
                for row in chosen['rows']:
                    writer.writerow([
                        '' if row.get(c['eng_name']) is None else (
                            'true' if row.get(c['eng_name']) is True else
                            'false' if row.get(c['eng_name']) is False else
                            row.get(c['eng_name'])
                        )
                        for c in export_cols
                    ])
            text = buf.getvalue()
            enc = target_encoding or session.encoding or 'utf-8-sig'
            if enc not in ('utf-8', 'utf-8-sig', 'gb18030', 'utf-16'):
                enc = 'utf-8-sig'
            try:
                raw_bytes = text.encode(enc)
            except UnicodeEncodeError:
                enc = 'utf-8-sig'
                raw_bytes = text.encode(enc)
            media = 'text/tab-separated-values; charset=utf-8' if (session.csv_delimiter == '\t') else 'text/csv; charset=utf-8'
            return {
                'filename': session.filename,
                'kind': 'csv',
                'encoding': enc,
                'media_type': media,
                'bytes': raw_bytes,
                'text': text,
            }

        # Excel (.xlsx / .xls) 多 Sheet 导出
        sheets: list[dict] = []
        seen_sheet_names: set[str] = set()
        for idx, item in enumerate(tables, 1):
            head = item['definition']['head']
            meta = item['meta']
            raw_sheet_title = (head.get('chn_name') or '').strip() or head['eng_name']
            sheet_title = _sanitize_sheet_name(raw_sheet_title, seen_sheet_names, idx)

            columns = item['definition']['columns']
            skip_synthetic = bool(meta.get('synthetic_pk'))
            export_cols = [c for c in columns if not (skip_synthetic and c['eng_name'] == SYNTHETIC_PK_COLUMN)]
            if not export_cols:
                export_cols = list(columns)

            header_row = [
                (c.get('chn_name') or '').strip() or c['eng_name']
                for c in export_cols
            ]
            rows_out: list[list] = [header_row]
            for row in item['rows']:
                rows_out.append([row.get(c['eng_name']) for c in export_cols])
            sheets.append({'name': sheet_title, 'rows': rows_out})

        if kind == 'xlsx':
            raw_bytes = _write_xlsx_sheets(sheets)
            media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            out_name = session.filename if session.filename.lower().endswith('.xlsx') else Path(session.filename).stem + '.xlsx'
        else:
            raw_bytes = write_xls_sheets(sheets)
            media = 'application/vnd.ms-excel'
            out_name = session.filename if session.filename.lower().endswith('.xls') else Path(session.filename).stem + '.xls'

        return {
            'filename': out_name,
            'kind': kind,
            'encoding': kind.upper(),
            'media_type': media,
            'bytes': raw_bytes,
            'text': None,
        }

    @staticmethod
    def _serialize_efile(session: LocalFileSession, tables: list[dict]) -> str:
        """按标准电力 E 文件语法无损序列化所有表。"""
        lines: list[str] = list(session.efile_header)
        for item in tables:
            head = item['definition']['head']
            meta = item['meta']
            eng_name = head['eng_name']
            if meta.get('open_line'):
                open_line = meta['open_line']
                close_line = meta.get('close_line') or f'</{meta.get("full_tag") or eng_name}>'
            else:
                open_line = f'<{eng_name}>'
                close_line = f'</{eng_name}>'

            lines.append(open_line)
            columns = item['definition']['columns']
            skip_synthetic = bool(meta.get('synthetic_pk'))
            export_cols = [c for c in columns if not (skip_synthetic and c['eng_name'] == SYNTHETIC_PK_COLUMN)]
            if not export_cols:
                export_cols = list(columns)

            col_orig = meta.get('col_original_names') or {}
            header_names = [col_orig.get(c['eng_name'], c['eng_name']) for c in export_cols]
            lines.append('@ ' + ' '.join(header_names))

            need_desc = bool(meta.get('has_desc_line')) or any(
                (c.get('chn_name') or '').strip() not in ('', c['eng_name'], col_orig.get(c['eng_name'], c['eng_name']))
                for c in export_cols
            )
            if need_desc:
                desc_tokens = [
                    _format_efile_comment_token(c.get('chn_name') or col_orig.get(c['eng_name'], c['eng_name']))
                    for c in export_cols
                ]
                lines.append('// ' + ' '.join(desc_tokens))

            for comment in meta.get('extra_comments') or []:
                lines.append(comment)

            for row in item['rows']:
                vals = [_format_efile_token(row.get(c['eng_name'])) for c in export_cols]
                lines.append('# ' + ' '.join(vals))

            lines.append(close_line)

        nl = session.efile_newline or '\n'
        return nl.join(lines) + nl
