"""Read legacy dbdef / E exchange files as data, and emit Python definitions."""
from __future__ import annotations

import base64
import copy
import keyword
import math
import re

from .table_schema import SchemaError


def normalize_definition_labels(definition):
    """Default missing/null Chinese names on writes, preserving stored fingerprints on reads."""
    if not isinstance(definition, dict):
        return definition
    result = copy.deepcopy(definition)
    items = [result.get('head')]
    if isinstance(result.get('columns'), list):
        items.extend(result['columns'])
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get('chn_name')
        if label is None or (isinstance(label, str) and label.strip().lower() in ('', 'null', r'\n')):
            item['chn_name'] = item.get('eng_name', '')
    return result


def decode_upload(payload):
    if isinstance(payload.get('text'), str):
        return payload['text'].lstrip('\ufeff'), 'text'
    try:
        data = base64.b64decode(payload.get('content', ''), validate=True)
    except (ValueError, TypeError) as error:
        raise SchemaError('文件内容不是有效的 Base64') from error
    if not data or len(data) > 20 * 1024 * 1024:
        raise SchemaError('请选择不超过 20 MiB 的非空文件')
    encoding = payload.get('encoding', 'auto')
    if encoding not in ('auto', 'utf-8-sig', 'gb18030', 'utf-16'):
        raise SchemaError('不支持的文件编码')
    candidates = ([encoding] if encoding != 'auto' else
                  ['utf-16'] if data.startswith((b'\xff\xfe', b'\xfe\xff')) else ['utf-8-sig', 'gb18030'])
    for candidate in candidates:
        try:
            return data.decode(candidate).lstrip('\ufeff'), candidate
        except UnicodeError:
            pass
    raise SchemaError('无法解码文件，请选择正确的文本编码')


def tokens(text):
    """Whitespace tokens, quoted empty strings, doubled quotes and multiline text."""
    result, i = [], 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        value = ''
        quote = text[i] if text[i] in "\"'" else None
        if quote:
            i += 1
            while i < len(text):
                if text[i] == quote:
                    if i + 1 < len(text) and text[i + 1] == quote:
                        value += quote
                        i += 2
                        continue
                    i += 1
                    break
                value += text[i]
                i += 1
            else:
                raise SchemaError('引号未闭合')
            if i < len(text) and not text[i].isspace():
                raise SchemaError('引号结束后缺少分隔空白')
        else:
            start = i
            while i < len(text) and not text[i].isspace():
                i += 1
            value = text[start:i]
        result.append(value)
    return result


def logical_lines(text):
    line, quote, start = '', None, 1
    for number, raw in enumerate(text.splitlines(keepends=True), 1):
        if not line:
            start = number
        i = 0
        while i < len(raw):
            ch = raw[i]
            if quote and ch == quote:
                if i + 1 < len(raw) and raw[i + 1] == quote:
                    line += ch * 2
                    i += 2
                    continue
                quote = None
            elif not quote and ch in "\"'" and (not line or line[-1].isspace() or line[-1] in '=@#'):
                quote = ch
            line += ch
            i += 1
        if not quote:
            yield start, line.strip()
            line = ''
    if quote:
        raise SchemaError(f'第 {start} 行：引号未闭合')


def parse_modules(text, *, dbdef=False):
    modules, current = [], None
    for number, line in logical_lines(text):
        if not line or line.startswith('<!') or (line.startswith('//') and current is None):
            continue
        if line.startswith('</'):
            if current is None or line[2:-1].strip() != current['tag']:
                raise SchemaError(f'第 {number} 行：结束标签不匹配')
            if not current['columns']:
                raise SchemaError(f"{current['name']} 缺少 @ 字段定义")
            current = None
        elif line.startswith('<') and line.endswith('>'):
            if current:
                raise SchemaError(f'第 {number} 行：不支持嵌套模块')
            body = line[1:-1].strip()
            tag = body.split()[0]
            name = tag.split('::')[0]
            if any(module['name'] == name for module in modules):
                raise SchemaError(f'重复模块：{name}（不同用途的同名模块请分开导入）')
            props = {m[1]: (m[2] or m[3] or m[4]) for m in re.finditer(
                r'''(\w+)\s*=\s*(?:'([^']*)'|"([^"]*)"|([^\s]+))''', body)}
            current = dict(name=name, tag=tag, columns=[], descriptions=[], rows=[], props=props)
            modules.append(current)
        elif current and line.startswith('@'):
            if current['columns']:
                raise SchemaError(f'第 {number} 行：重复 @ 字段定义')
            current['columns'] = tokens(line[1:])
            if dbdef and current['name'] == 'table_info' and 'table_name_eng' not in current['columns']:
                # Compact dbdef variant stores table metadata directly on the @ line.
                values = current['columns']
                if len(values) != 7:
                    raise SchemaError(f'第 {number} 行：简写 table_info 应包含 7 个值')
                current['columns'] = ['table_name_eng', 'table_name_chn', 'app_type', 'max_record_num', 'down_load', 'is_model', 'module_name']
                current['rows'].append((number, values))
            if len(set(current['columns'])) != len(current['columns']):
                raise SchemaError(f'第 {number} 行：字段名重复')
        elif current and line.startswith('//'):
            current['descriptions'] = tokens(line[2:])
        elif current and line.startswith('#'):
            if not current['columns']:
                raise SchemaError(f'第 {number} 行：数据之前缺少 @ 字段定义')
            current['rows'].append((number, tokens(line[1:])))
        else:
            raise SchemaError(f'第 {number} 行：无法识别的 E 文件内容')
    if current:
        raise SchemaError(f"{current['name']} 缺少结束标签")
    if not modules:
        raise SchemaError('文件中未找到数据模块')
    return modules


TYPE_NAMES = {
    # The canonical values are the SQLite/web types used by the rest of the
    # importer.  The named aliases below intentionally collapse the legacy
    # RTDB types instead of preserving their width or storage spelling.
    'INT': 'int', 'INTEGER': 'int', 'LONG': 'int', 'SHORT': 'int', 'BYTE': 'int',
    'INT32': 'int', 'INT64': 'int', 'KEYID': 'int', 'UNSIGNED': 'int',
    'UCHAR': 'int', 'UINT': 'int', 'USHORT': 'int', 'ULONG': 'int', 'UINT32': 'int', 'UINT64': 'int',
    'APPKEYID': 'int',
    'FLOAT': 'float', 'DOUBLE': 'float', 'NUMBER': 'float', 'DECIMAL': 'float',
    'STRING': 'str', 'STR': 'str', 'TEXT': 'str', 'CHAR': 'str', 'BINARY': 'str', 'VARCHAR': 'str',
    'IMAGE': 'str', 'APPID': 'str', 'RTKEY': 'str', 'TH_RTKEY': 'str',
    # Legacy RTDB DATETIME values are integer timestamps in dbdef files.
    'DATETIME': 'int', 'DATE': 'str', 'TIME': 'str', 'BOOL': 'bool', 'BOOLEAN': 'bool',

    # TH_DATA_TYPE_T values from the legacy compatibility layer:
    # CHAR, STRING, UCHAR, SHORT, INT, FLOAT, DOUBLE, DATETIME, KEYID,
    # BINARY, TEXT, IMAGE, APPKEYID, APPID, LONG, RTKEY.
    '1': 'str', '2': 'str', '3': 'int', '4': 'int', '5': 'int',
    '6': 'float', '7': 'float', '8': 'int', '9': 'int', '10': 'str',
    '11': 'str', '12': 'str', '13': 'int', '14': 'str', '15': 'int',
    '16': 'str',
}


def canonical_data_type(value):
    """Return the canonical SQLite type for a legacy dbdef type value."""

    raw = str(value).strip()
    kind = TYPE_NAMES.get(raw.upper())
    if kind is None:
        raise SchemaError(f'不支持的字段类型 {raw}')
    return kind


def dbdef_definitions(modules):
    info = next((module for module in modules if module['name'] == 'table_info'), None)
    if info is None:
        raise SchemaError('dbdef 缺少 <table_info> 模块')
    if 'table_name_eng' not in info['columns'] or 'table_name_chn' not in info['columns']:
        raise SchemaError('table_info 缺少 table_name_eng / table_name_chn')
    names = [module['name'] for module in modules if module is not info]
    definitions = []
    for number, values in info['rows']:
        values = list(values)
        # Legacy fixed-width writers sometimes concatenate English/Chinese table names.
        index = info['columns'].index('table_name_eng')
        if len(values) > index and values[index] not in names:
            matches = [name for name in names if values[index].startswith(name)]
            if matches:
                name = max(matches, key=len)
                values[index:index + 1] = [name, values[index][len(name):]]
        row = dict(zip(info['columns'], values))
        name = row.get('table_name_eng', '')
        module = next((m for m in modules if m['name'] == name), None)
        if module is None:
            raise SchemaError(f'第 {number} 行：找不到 {name} 字段模块')
        columns = []
        for line, cells in module['rows']:
            cells = list(cells)
            if 'column_name_eng' in module['columns']:
                position = module['columns'].index('column_name_eng')
                if len(cells) > position:
                    joined = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)([\u3400-\u9fff].*)', cells[position])
                    if joined:
                        cells[position:position + 1] = [joined[1], joined[2]]
            if len(cells) > len(module['columns']):
                raise SchemaError(f'第 {line} 行：字段定义值过多；含空格的文字请用引号包围')
            item = dict(zip(module['columns'], cells))
            raw_kind = item.get('data_type', '')
            try:
                kind = canonical_data_type(raw_kind)
            except SchemaError as error:
                raise SchemaError(f'第 {line} 行：{error}') from error
            flag = item.get('is_key', '0').lower()
            if flag not in ('0', '1', 'true', 'false', 'yes', 'no'):
                raise SchemaError(f'第 {line} 行：无效的 is_key')
            field = item.get('column_name_eng', '')
            columns.append(dict(eng_name=field, chn_name=item.get('column_name_chn') or field,
                data_type=kind, primary_key=flag in ('1', 'true', 'yes'),
                disp_type=item.get('display_type') or raw_kind, disp_menu=item.get('menu_name', '')))
        definitions.append(dict(head=dict(eng_name=name, chn_name=row.get('table_name_chn') or name), columns=columns))
    if len({item['head']['eng_name'] for item in definitions}) != len(definitions):
        raise SchemaError('table_info 中存在重复表名')
    return definitions


def infer_type(values):
    values = [value for value in values if value not in ('', r'\N')]
    if not values:
        return 'str'
    if all(re.fullmatch(r'-?(?:0|[1-9]\d*)', value) for value in values):
        return 'int'
    if all(value.lower() in ('true', 'false') for value in values):
        return 'bool'
    try:
        if all(math.isfinite(float(value)) for value in values):
            # Preserve zero-prefixed identifiers instead of turning 001 into 1.0.
            if not any(re.fullmatch(r'-?0\d+', value) for value in values):
                return 'float'
    except ValueError:
        pass
    return 'str'


def python_definition(definition):
    """Generated code contains only repr-escaped literals; imported code is never run."""
    name = definition['head']['eng_name']
    class_name = name if not keyword.iskeyword(name) and not name.startswith('_') else 'TableRow'
    lines = ['# Generated by RTDB Web. Edit the JSON definition or use the schema editor.',
             'from __future__ import annotations',
             'from dataclasses import dataclass as _dataclass, field as _field',
             'from typing import Any as _Any, ClassVar as _ClassVar, Optional as _Optional', '',
             f'TABLE_DEFINITION = {definition!r}', '', '@_dataclass', f'class {class_name}:',
             f'    __tablename__: _ClassVar[str] = {name!r}',
             f'    __table_name_chn__: _ClassVar[str] = {definition["head"]["chn_name"]!r}',
             f'    __primary_keys__: _ClassVar[tuple] = {tuple(c["eng_name"] for c in definition["columns"] if c["primary_key"])!r}']
    used = {c['eng_name'] for c in definition['columns']}
    for i, column in enumerate(definition['columns']):
        field = column['eng_name']
        if keyword.iskeyword(field) or field.startswith('_'):
            field = f'field_{i}'
            while field in used:
                field += '_'
            used.add(field)
        kind = TYPE_NAMES.get(column['data_type'].upper(), '_Any')
        lines.append(f'    {field}: _Optional[{kind}] = _field(default=None, metadata={column!r})')
    lines.extend(['', f'Row = {class_name}', ''])
    source = '\n'.join(lines)
    compile(source, name + '.py', 'exec')
    return source
