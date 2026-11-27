"""无外部依赖的旧版 Excel (.xls) 读取与写出编解码器。

优先调用已安装的 xlrd / xlwt；若未安装，则使用内置纯 Python OLE2 + BIFF8 实现，
并自动兼容常见的伪 .xls 格式（重命名的 .xlsx、SpreadsheetML XML 与 HTML 表格）。
"""
from __future__ import annotations

import datetime
import io
import math
import re
import struct
import xml.etree.ElementTree as ET
from html.parser import HTMLParser


OLE2_SIGNATURE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
ZIP_SIGNATURE = b'PK\x03\x04'
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_FATSECT = 0xFFFFFFFD
_DIFSECT = 0xFFFFFFFC

# Excel 内置日期/时间数字格式编号集合
_BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(45, 48))
_DATE_FMT_RE = re.compile(r'[yYmMdDhHsS]')
_QUOTED_FMT_RE = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')


def _is_date_format(fmt_id: int, custom_formats: dict[int, str]) -> bool:
    """判断数字格式编号是否表示日期或时间。"""
    if fmt_id in _BUILTIN_DATE_FORMATS:
        return True
    fmt = custom_formats.get(fmt_id, '')
    if not fmt:
        return False
    cleaned = _QUOTED_FMT_RE.sub('', fmt)
    return bool(_DATE_FMT_RE.search(cleaned))


def _excel_serial_to_string(serial: float, datemode: int = 0) -> str:
    """将 Excel 日期序列号格式化为标准日期或日期时间字符串。"""
    if not math.isfinite(serial):
        return ''
    base = datetime.datetime(1904, 1, 1) if datemode else datetime.datetime(1899, 12, 30)
    try:
        dt = base + datetime.timedelta(days=float(serial))
    except OverflowError:
        return str(serial)
    if abs(serial - round(serial)) < 1e-6:
        return dt.strftime('%Y-%m-%d')
    if serial < 1.0:
        return dt.strftime('%H:%M:%S')
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _normalize_number(value: float):
    """将无小数部分的浮点数还原为整数，避免 Excel 数字全带 .0。"""
    if not math.isfinite(value):
        return None
    rounded = round(value)
    if abs(value - rounded) <= 1e-9 and abs(rounded) <= (1 << 53):
        return int(rounded)
    return value


def _decode_rk(rk: int) -> float:
    """解码 BIFF8 32 位 RK 压缩数值。"""
    is_div100 = bool(rk & 0x01)
    is_int = bool(rk & 0x02)
    if is_int:
        raw = rk >> 2
        if raw & 0x20000000:
            raw -= 0x40000000
        value = float(raw)
    else:
        raw_ieee = struct.pack('<II', 0, rk & 0xFFFFFFFC)
        value = struct.unpack('<d', raw_ieee)[0]
    if is_div100:
        value /= 100.0
    return value


def _codepage_encoding(codepage: int) -> str:
    """将 BIFF8 CODEPAGE 编号映射为 Python 编解码名称。"""
    mapping = {
        936: 'gb18030',
        950: 'big5',
        932: 'cp932',
        949: 'cp949',
        1200: 'utf-16-le',
        1250: 'cp1250',
        1251: 'cp1251',
        1252: 'cp1252',
        1253: 'cp1253',
        1254: 'cp1254',
        1255: 'cp1255',
        1256: 'cp1256',
        1257: 'cp1257',
        1258: 'cp1258',
        65001: 'utf-8',
    }
    return mapping.get(codepage, 'cp1252')


# ---------------------------------------------------------------------------
# OLE2 (Compound Document) 读取
# ---------------------------------------------------------------------------

def _extract_ole2_workbook_stream(data: bytes) -> bytes:
    """从 OLE2 复合文档二进制字节中提取 Workbook / Book 流。"""
    if len(data) < 512 or data[:8] != OLE2_SIGNATURE:
        raise ValueError('不是有效的 OLE2 .xls 文件')

    sector_shift = struct.unpack_from('<H', data, 30)[0]
    mini_sector_shift = struct.unpack_from('<H', data, 32)[0]
    sector_size = 1 << sector_shift
    mini_sector_size = 1 << mini_sector_shift
    first_dir_sector = struct.unpack_from('<I', data, 48)[0]
    mini_cutoff = struct.unpack_from('<I', data, 56)[0]
    first_minifat_sector = struct.unpack_from('<I', data, 60)[0]
    num_minifat_sectors = struct.unpack_from('<I', data, 64)[0]
    first_difat_sector = struct.unpack_from('<I', data, 68)[0]
    num_difat_sectors = struct.unpack_from('<I', data, 72)[0]

    def read_sector(sec_id: int) -> bytes:
        offset = (sec_id + 1) * sector_size
        return data[offset:offset + sector_size]

    # 1. 收集所有 FAT 扇区号（头部 109 项 + DIFAT 链）
    fat_sectors = [
        sec for sec in struct.unpack_from('<109I', data, 76)
        if sec not in (_FREESECT, _ENDOFCHAIN, _FATSECT, _DIFSECT)
    ]
    difat_sec = first_difat_sector
    entries_per_sector = sector_size // 4
    visited_difat = set()
    for _ in range(num_difat_sectors):
        if difat_sec in (_FREESECT, _ENDOFCHAIN) or difat_sec in visited_difat:
            break
        visited_difat.add(difat_sec)
        raw = read_sector(difat_sec)
        if len(raw) < sector_size:
            break
        ints = struct.unpack(f'<{entries_per_sector}I', raw)
        for sec in ints[:-1]:
            if sec not in (_FREESECT, _ENDOFCHAIN, _FATSECT, _DIFSECT):
                fat_sectors.append(sec)
        difat_sec = ints[-1]

    # 2. 构建 FAT 链映射表
    fat: list[int] = []
    for sec in fat_sectors:
        raw = read_sector(sec)
        if len(raw) < sector_size:
            raw = raw.ljust(sector_size, b'\xff')
        fat.extend(struct.unpack(f'<{entries_per_sector}I', raw))

    def read_chain(start_sec: int, max_size: int | None = None) -> bytes:
        chunks = []
        sec = start_sec
        seen = set()
        total = 0
        while sec not in (_ENDOFCHAIN, _FREESECT) and sec < len(fat) and sec not in seen:
            seen.add(sec)
            chunk = read_sector(sec)
            chunks.append(chunk)
            total += len(chunk)
            if max_size is not None and total >= max_size:
                break
            sec = fat[sec]
        joined = b''.join(chunks)
        return joined[:max_size] if max_size is not None else joined

    # 3. 读取目录项并寻找 Root Entry 与 Workbook / Book
    dir_bytes = read_chain(first_dir_sector)
    root_start = _ENDOFCHAIN
    root_size = 0
    target_entry = None

    for offset in range(0, len(dir_bytes) - 127, 128):
        entry = dir_bytes[offset:offset + 128]
        name_len = struct.unpack_from('<H', entry, 64)[0]
        obj_type = entry[66]
        if obj_type == 0 or name_len < 2 or name_len > 64:
            continue
        name = entry[:name_len - 2].decode('utf-16-le', errors='ignore')
        start_sec = struct.unpack_from('<I', entry, 116)[0]
        stream_size = struct.unpack_from('<I', entry, 120)[0]
        if obj_type == 5:  # Root Storage
            root_start, root_size = start_sec, stream_size
        elif obj_type == 2 and name.lower() in ('workbook', 'book'):
            target_entry = (name, start_sec, stream_size)
            if name.lower() == 'workbook':
                break

    if target_entry is None:
        raise ValueError('OLE2 文件中未找到 Workbook 数据流')

    _, wb_start, wb_size = target_entry
    if wb_size >= mini_cutoff or first_minifat_sector in (_ENDOFCHAIN, _FREESECT) or num_minifat_sectors == 0:
        return read_chain(wb_start, wb_size)

    # 4. 小流走 MiniFAT + Root MiniStream
    mini_stream = read_chain(root_start, root_size)
    minifat_bytes = read_chain(first_minifat_sector, num_minifat_sectors * sector_size)
    minifat = struct.unpack(f'<{len(minifat_bytes) // 4}I', minifat_bytes)
    chunks = []
    sec = wb_start
    seen = set()
    total = 0
    while sec not in (_ENDOFCHAIN, _FREESECT) and sec < len(minifat) and sec not in seen:
        seen.add(sec)
        pos = sec * mini_sector_size
        chunks.append(mini_stream[pos:pos + mini_sector_size])
        total += mini_sector_size
        if total >= wb_size:
            break
        sec = minifat[sec]
    return b''.join(chunks)[:wb_size]


# ---------------------------------------------------------------------------
# BIFF8 记录流解析
# ---------------------------------------------------------------------------

def _iter_biff_records(stream: bytes, start_offset: int = 0):
    """按顺序迭代 BIFF8 记录：(offset, rec_id, payload)。"""
    pos = start_offset
    limit = len(stream)
    while pos + 4 <= limit:
        rec_id, rec_len = struct.unpack_from('<HH', stream, pos)
        data_start = pos + 4
        data_end = min(data_start + rec_len, limit)
        yield pos, rec_id, stream[data_start:data_end]
        pos = data_end


def _parse_biff8_short_string(payload: bytes, offset: int, encoding: str) -> tuple[str, int]:
    """解析 BIFF8 1 字节长度的 Unicode 字符串（用于 BOUNDSHEET）。"""
    if offset + 2 > len(payload):
        return '', len(payload)
    nchars = payload[offset]
    flags = payload[offset + 1]
    pos = offset + 2
    high_byte = bool(flags & 0x01)
    if high_byte:
        end = pos + nchars * 2
        text = payload[pos:end].decode('utf-16-le', errors='replace')
    else:
        end = pos + nchars
        text = payload[pos:end].decode(encoding, errors='replace')
    return text, end


def _parse_biff8_xlstring(payload: bytes, offset: int, encoding: str) -> tuple[str, int]:
    """解析 BIFF8 2 字节长度的 XLUnicodeRichExtendedString。"""
    if offset + 3 > len(payload):
        return '', len(payload)
    nchars = struct.unpack_from('<H', payload, offset)[0]
    flags = payload[offset + 2]
    pos = offset + 3
    high_byte = bool(flags & 0x01)
    has_ext = bool(flags & 0x04)
    has_rich = bool(flags & 0x08)
    rich_runs = 0
    ext_len = 0
    if has_rich:
        rich_runs = struct.unpack_from('<H', payload, pos)[0]
        pos += 2
    if has_ext:
        ext_len = struct.unpack_from('<I', payload, pos)[0]
        pos += 4
    if high_byte:
        end = pos + nchars * 2
        text = payload[pos:end].decode('utf-16-le', errors='replace')
    else:
        end = pos + nchars
        text = payload[pos:end].decode(encoding, errors='replace')
    pos = end + rich_runs * 4 + ext_len
    return text, pos


def _parse_sst(chunks: list[bytes]) -> list[str]:
    """解析支持跨 CONTINUE 记录拆分的 BIFF8 SST 共享字符串表。"""
    if not chunks or len(chunks[0]) < 8:
        return []
    unique_count = struct.unpack_from('<I', chunks[0], 4)[0]
    chunk_idx = 0
    pos = 8
    strings: list[str] = []

    def advance_chunk() -> bool:
        nonlocal chunk_idx, pos
        chunk_idx += 1
        pos = 0
        return chunk_idx < len(chunks)

    def read_bytes(count: int) -> bytes:
        nonlocal pos
        out = bytearray()
        need = count
        while need > 0 and chunk_idx < len(chunks):
            cur = chunks[chunk_idx]
            avail = len(cur) - pos
            if avail <= 0:
                if not advance_chunk():
                    break
                continue
            take = min(need, avail)
            out.extend(cur[pos:pos + take])
            pos += take
            need -= take
        return bytes(out)

    for _ in range(unique_count):
        if chunk_idx >= len(chunks):
            break
        if pos >= len(chunks[chunk_idx]) and not advance_chunk():
            break
        header = read_bytes(3)
        if len(header) < 3:
            break
        nchars = struct.unpack_from('<H', header, 0)[0]
        flags = header[2]
        high_byte = bool(flags & 0x01)
        has_ext = bool(flags & 0x04)
        has_rich = bool(flags & 0x08)
        rich_runs = struct.unpack('<H', read_bytes(2))[0] if has_rich else 0
        ext_len = struct.unpack('<I', read_bytes(4))[0] if has_ext else 0

        parts: list[str] = []
        remaining = nchars
        while remaining > 0 and chunk_idx < len(chunks):
            if pos >= len(chunks[chunk_idx]):
                if not advance_chunk():
                    break
                # 跨 CONTINUE 拆分字符串时，新块首字节是当前片段的高低字节编码标志
                if pos < len(chunks[chunk_idx]):
                    high_byte = bool(chunks[chunk_idx][pos] & 0x01)
                    pos += 1
            cur = chunks[chunk_idx]
            avail = len(cur) - pos
            char_size = 2 if high_byte else 1
            take_chars = min(remaining, avail // char_size)
            if take_chars <= 0:
                if not advance_chunk():
                    break
                continue
            raw_piece = cur[pos:pos + take_chars * char_size]
            pos += take_chars * char_size
            remaining -= take_chars
            if high_byte:
                parts.append(raw_piece.decode('utf-16-le', errors='replace'))
            else:
                parts.append(raw_piece.decode('latin-1', errors='replace'))

        # 跳过富文本格式与扩展拼音数据
        skip = rich_runs * 4 + ext_len
        if skip > 0:
            read_bytes(skip)
        strings.append(''.join(parts))

    return strings


def _read_biff8_workbook(stream: bytes) -> list[dict]:
    """解析 BIFF8 Workbook 字节流并返回全部工作表数据。"""
    encoding = 'cp1252'
    datemode = 0
    custom_formats: dict[int, str] = {}
    xf_format_ids: list[int] = []
    bound_sheets: list[tuple[str, int]] = []
    sst: list[str] = []

    # 1. 解析工作簿全局段（首个 BOF 到首个 EOF）
    records = list(_iter_biff_records(stream, 0))
    idx = 0
    while idx < len(records):
        _, rec_id, payload = records[idx]
        if rec_id == 0x000A:  # EOF
            break
        if rec_id == 0x0042 and len(payload) >= 2:  # CODEPAGE
            codepage = struct.unpack_from('<H', payload, 0)[0]
            encoding = _codepage_encoding(codepage)
        elif rec_id == 0x0022 and len(payload) >= 2:  # DATEMODE
            datemode = struct.unpack_from('<H', payload, 0)[0]
        elif rec_id == 0x041E and len(payload) >= 5:  # FORMAT
            fmt_id = struct.unpack_from('<H', payload, 0)[0]
            fmt_str, _ = _parse_biff8_xlstring(payload, 2, encoding)
            custom_formats[fmt_id] = fmt_str
        elif rec_id == 0x00E0 and len(payload) >= 4:  # XF
            xf_format_ids.append(struct.unpack_from('<H', payload, 2)[0])
        elif rec_id == 0x0085 and len(payload) >= 8:  # BOUNDSHEET
            bof_offset = struct.unpack_from('<I', payload, 0)[0]
            sheet_type = payload[5]
            name, _ = _parse_biff8_short_string(payload, 6, encoding)
            # 0x00 为普通工作表（Worksheet）
            if sheet_type == 0x00:
                bound_sheets.append((name or f'Sheet{len(bound_sheets) + 1}', bof_offset))
        elif rec_id == 0x00FC:  # SST + 后续所有 CONTINUE
            sst_chunks = [payload]
            while idx + 1 < len(records) and records[idx + 1][1] == 0x003C:
                idx += 1
                sst_chunks.append(records[idx][2])
            sst = _parse_sst(sst_chunks)
        idx += 1

    def format_num(val: float, xf_idx: int):
        fmt_id = xf_format_ids[xf_idx] if 0 <= xf_idx < len(xf_format_ids) else 0
        if _is_date_format(fmt_id, custom_formats):
            return _excel_serial_to_string(val, datemode)
        return _normalize_number(val)

    # 2. 逐工作表解析单元格
    sheets: list[dict] = []
    for sheet_name, bof_offset in bound_sheets:
        cells: dict[tuple[int, int], object] = {}
        max_row = -1
        max_col = -1
        pending_formula_cell: tuple[int, int] | None = None

        for _, rec_id, payload in _iter_biff_records(stream, bof_offset):
            if rec_id == 0x000A:  # EOF
                break
            if rec_id == 0x0207 and pending_formula_cell is not None:  # STRING (formula text result)
                text, _ = _parse_biff8_xlstring(payload, 0, encoding)
                r, c = pending_formula_cell
                cells[(r, c)] = text
                max_row = max(max_row, r)
                max_col = max(max_col, c)
                pending_formula_cell = None
                continue
            pending_formula_cell = None

            if rec_id == 0x0203 and len(payload) >= 14:  # NUMBER
                r, c, xf_idx = struct.unpack_from('<HHH', payload, 0)
                num = struct.unpack_from('<d', payload, 6)[0]
                cells[(r, c)] = format_num(num, xf_idx)
                max_row = max(max_row, r)
                max_col = max(max_col, c)
            elif rec_id == 0x027E and len(payload) >= 10:  # RK
                r, c, xf_idx = struct.unpack_from('<HHH', payload, 0)
                rk = struct.unpack_from('<I', payload, 6)[0]
                cells[(r, c)] = format_num(_decode_rk(rk), xf_idx)
                max_row = max(max_row, r)
                max_col = max(max_col, c)
            elif rec_id == 0x00BD and len(payload) >= 10:  # MULRK
                r, first_col = struct.unpack_from('<HH', payload, 0)
                last_col = struct.unpack_from('<H', payload, len(payload) - 2)[0]
                pos = 4
                for col in range(first_col, last_col + 1):
                    if pos + 6 > len(payload) - 2:
                        break
                    xf_idx = struct.unpack_from('<H', payload, pos)[0]
                    rk = struct.unpack_from('<I', payload, pos + 2)[0]
                    cells[(r, col)] = format_num(_decode_rk(rk), xf_idx)
                    max_row = max(max_row, r)
                    max_col = max(max_col, col)
                    pos += 6
            elif rec_id == 0x00FD and len(payload) >= 10:  # LABELSST
                r, c, _ = struct.unpack_from('<HHH', payload, 0)
                sst_idx = struct.unpack_from('<I', payload, 6)[0]
                cells[(r, c)] = sst[sst_idx] if 0 <= sst_idx < len(sst) else ''
                max_row = max(max_row, r)
                max_col = max(max_col, c)
            elif rec_id in (0x0204, 0x00D6) and len(payload) >= 9:  # LABEL / RSTRING
                r, c, _ = struct.unpack_from('<HHH', payload, 0)
                text, _ = _parse_biff8_xlstring(payload, 6, encoding)
                cells[(r, c)] = text
                max_row = max(max_row, r)
                max_col = max(max_col, c)
            elif rec_id == 0x0205 and len(payload) >= 8:  # BOOLERR
                r, c, _ = struct.unpack_from('<HHH', payload, 0)
                val_byte, is_err = payload[6], payload[7]
                cells[(r, c)] = None if is_err else bool(val_byte)
                max_row = max(max_row, r)
                max_col = max(max_col, c)
            elif rec_id == 0x0006 and len(payload) >= 20:  # FORMULA
                r, c, xf_idx = struct.unpack_from('<HHH', payload, 0)
                result_bytes = payload[6:14]
                if result_bytes[6:8] == b'\xff\xff':
                    kind = result_bytes[0]
                    if kind == 0x00:  # 字符串结果位于紧随的 STRING 记录
                        pending_formula_cell = (r, c)
                    elif kind == 0x01:  # 布尔值
                        cells[(r, c)] = bool(result_bytes[2])
                        max_row = max(max_row, r)
                        max_col = max(max_col, c)
                    elif kind == 0x03:  # 空字符串
                        cells[(r, c)] = ''
                        max_row = max(max_row, r)
                        max_col = max(max_col, c)
                else:
                    num = struct.unpack('<d', result_bytes)[0]
                    cells[(r, c)] = format_num(num, xf_idx)
                    max_row = max(max_row, r)
                    max_col = max(max_col, c)

        rows: list[list[object]] = []
        if max_row >= 0 and max_col >= 0:
            for r in range(max_row + 1):
                rows.append([cells.get((r, c)) for c in range(max_col + 1)])
        sheets.append({'name': sheet_name, 'rows': rows})

    return sheets


# ---------------------------------------------------------------------------
# 伪 .xls（SpreadsheetML XML / HTML Table）解析兜底
# ---------------------------------------------------------------------------

def _parse_spreadsheetml_xml(text: str) -> list[dict]:
    """解析 Excel 2003 XML (SpreadsheetML) 多 Sheet 文件。"""
    root = ET.fromstring(text)
    sheets: list[dict] = []
    for elem in root.iter():
        if not elem.tag.endswith('Worksheet'):
            continue
        sheet_name = None
        for attr_key, attr_val in elem.attrib.items():
            if attr_key.endswith('Name'):
                sheet_name = attr_val
                break
        rows: list[list[object]] = []
        for table in elem:
            if not table.tag.endswith('Table'):
                continue
            for row_el in table:
                if not row_el.tag.endswith('Row'):
                    continue
                row_vals: list[object] = []
                col_idx = 1
                for cell in row_el:
                    if not cell.tag.endswith('Cell'):
                        continue
                    explicit_idx = None
                    for ak, av in cell.attrib.items():
                        if ak.endswith('Index'):
                            try:
                                explicit_idx = int(av)
                            except ValueError:
                                explicit_idx = None
                    if explicit_idx is not None and explicit_idx > col_idx:
                        row_vals.extend([None] * (explicit_idx - col_idx))
                        col_idx = explicit_idx
                    val = None
                    for data_el in cell:
                        if data_el.tag.endswith('Data'):
                            raw = data_el.text if data_el.text is not None else ''
                            dtype = ''
                            for ak, av in data_el.attrib.items():
                                if ak.endswith('Type'):
                                    dtype = av.lower()
                            if dtype == 'number':
                                try:
                                    val = _normalize_number(float(raw))
                                except ValueError:
                                    val = raw
                            elif dtype == 'boolean':
                                val = raw in ('1', 'true', 'True')
                            else:
                                val = raw
                            break
                    row_vals.append(val)
                    col_idx += 1
                rows.append(row_vals)
        sheets.append({'name': sheet_name or f'Sheet{len(sheets) + 1}', 'rows': rows})
    return sheets


class _HTMLTableParser(HTMLParser):
    """提取伪装成 .xls 的 HTML 表格。"""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == 'table':
            self._table = []
        elif t == 'tr' and self._table is not None:
            self._row = []
        elif t in ('td', 'th') and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ('td', 'th') and self._cell is not None and self._row is not None:
            self._row.append(''.join(self._cell).strip())
            self._cell = None
        elif t == 'tr' and self._row is not None and self._table is not None:
            if any(cell != '' for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif t == 'table' and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def read_xls_sheets(data: bytes) -> list[dict]:
    """读取 .xls 二进制内容，返回 [{'name': str, 'rows': list[list]}] 列表。"""
    if not data:
        raise ValueError('空的 .xls 文件')

    # 1. 重命名成 .xls 的 .xlsx (ZIP)
    if data[:4] == ZIP_SIGNATURE:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        try:
            sheets = []
            for title in wb.sheetnames:
                ws = wb[title]
                rows = [list(row) for row in ws.iter_rows(values_only=True)]
                sheets.append({'name': title, 'rows': rows})
            return sheets
        finally:
            wb.close()

    # 2. 标准 OLE2 + BIFF8 二进制 .xls
    if data[:8] == OLE2_SIGNATURE:
        try:
            import xlrd  # type: ignore[import-not-found]
        except ImportError:
            xlrd = None
        if xlrd is not None:
            book = xlrd.open_workbook(file_contents=data)
            sheets = []
            for sheet in book.sheets():
                rows = []
                for r in range(sheet.nrows):
                    row_vals = []
                    for c in range(sheet.ncols):
                        cell = sheet.cell(r, c)
                        if cell.ctype == xlrd.XL_CELL_EMPTY:
                            row_vals.append(None)
                        elif cell.ctype == xlrd.XL_CELL_DATE:
                            row_vals.append(_excel_serial_to_string(cell.value, book.datemode))
                        elif cell.ctype == xlrd.XL_CELL_NUMBER:
                            row_vals.append(_normalize_number(cell.value))
                        elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                            row_vals.append(bool(cell.value))
                        elif cell.ctype == xlrd.XL_CELL_ERROR:
                            row_vals.append(None)
                        else:
                            row_vals.append(cell.value)
                    rows.append(row_vals)
                sheets.append({'name': sheet.name, 'rows': rows})
            return sheets
        wb_stream = _extract_ole2_workbook_stream(data)
        return _read_biff8_workbook(wb_stream)

    # 3. 兼容伪装成 .xls 的 SpreadsheetML XML 或 HTML Table
    text = None
    for enc in ('utf-8-sig', 'gb18030', 'utf-16'):
        try:
            text = data.decode(enc)
            break
        except UnicodeError:
            continue
    if text is None:
        raise ValueError('无法识别的 .xls 文件格式')
    stripped = text.lstrip()
    if stripped.startswith('<?xml') or '<Workbook' in stripped[:512]:
        sheets = _parse_spreadsheetml_xml(stripped)
        if sheets:
            return sheets
    if '<table' in stripped.lower():
        parser = _HTMLTableParser()
        parser.feed(stripped)
        if parser.tables:
            return [{'name': f'Sheet{i + 1}', 'rows': tbl} for i, tbl in enumerate(parser.tables)]
    raise ValueError('不支持的 .xls 文件格式或文件已损坏')


# ---------------------------------------------------------------------------
# 标准 OLE2 + BIFF8 二进制 .xls 写出器
# ---------------------------------------------------------------------------

def _biff_record(rec_id: int, payload: bytes) -> bytes:
    return struct.pack('<HH', rec_id, len(payload)) + payload


def _encode_biff8_short_utf16(text: str) -> bytes:
    """编码 BOUNDSHEET 用的 UTF-16LE 短名称（最多 31 字符）。"""
    safe = text[:31]
    return struct.pack('<BB', len(safe), 0x01) + safe.encode('utf-16-le')


def _build_sst_records(strings: list[str], total_refs: int) -> bytes:
    """生成带自动 CONTINUE 拆分的 BIFF8 SST 记录。"""
    max_rec = 8220
    out = bytearray()
    buf = bytearray(struct.pack('<II', total_refs, len(strings)))
    rec_id = 0x00FC

    for text in strings:
        encoded = text.encode('utf-16-le')
        header = struct.pack('<HB', len(text), 0x01)
        # 若剩余空间放不下头 + 至少 1 个字符，先落盘当前记录并开启 CONTINUE
        if len(buf) + len(header) + (2 if encoded else 0) > max_rec:
            out.extend(_biff_record(rec_id, bytes(buf)))
            rec_id = 0x003C
            buf = bytearray()
        buf.extend(header)
        pos = 0
        while pos < len(encoded):
            avail = max_rec - len(buf)
            if avail < 2:
                out.extend(_biff_record(rec_id, bytes(buf)))
                rec_id = 0x003C
                buf = bytearray(b'\x01')  # CONTINUE 续接 UTF-16LE 标志
                avail = max_rec - 1
            take = min(len(encoded) - pos, (avail // 2) * 2)
            buf.extend(encoded[pos:pos + take])
            pos += take

    out.extend(_biff_record(rec_id, bytes(buf)))
    return bytes(out)


def _wrap_ole2_workbook(workbook_stream: bytes) -> bytes:
    """将 Workbook 字节流封装为标准 512 字节扇区对齐的 OLE2 复合文档。"""
    sector_size = 512
    # 保证流长度 >= 4096 字节，始终走常规 FAT 链而无需构造 MiniStream
    raw_len = len(workbook_stream)
    padded_len = max(4096, ((raw_len + sector_size - 1) // sector_size) * sector_size)
    padded_stream = workbook_stream.ljust(padded_len, b'\x00')
    wb_sectors = padded_len // sector_size

    # 扇区布局：[0 .. wb_sectors-1] = Workbook 流；[wb_sectors] = Directory；[wb_sectors+1 ..] = FAT 扇区
    dir_sector = wb_sectors
    entries_per_fat = sector_size // 4
    # 计算所需 FAT 扇区数（固定点迭代）
    num_fat = 1
    while True:
        total_sectors = wb_sectors + 1 + num_fat
        needed = (total_sectors + entries_per_fat - 1) // entries_per_fat
        if needed == num_fat:
            break
        num_fat = needed

    fat_start = dir_sector + 1
    fat_entries: list[int] = []
    # Workbook 扇区链
    for i in range(wb_sectors):
        fat_entries.append(i + 1 if i + 1 < wb_sectors else _ENDOFCHAIN)
    # Directory 扇区
    fat_entries.append(_ENDOFCHAIN)
    # FAT 自身扇区标记
    for _ in range(num_fat):
        fat_entries.append(_FATSECT)
    # 填充空闲项
    while len(fat_entries) < num_fat * entries_per_fat:
        fat_entries.append(_FREESECT)

    # 构造 DIFAT（超过 109 个 FAT 扇区即 > 7 MiB 的超大流另行挂载，此处支持 <= 109 个 FAT 即 6.8 MiB+）
    if num_fat > 109:
        raise ValueError('生成的 .xls 文件超出内置 BIFF8 大小上限，请改用 .xlsx 格式保存')
    difat = [fat_start + i for i in range(num_fat)] + [_FREESECT] * (109 - num_fat)

    # 1. OLE2 512 字节头
    header = bytearray(512)
    header[0:8] = OLE2_SIGNATURE
    struct.pack_into('<H', header, 24, 0x003E)          # Minor Version
    struct.pack_into('<H', header, 26, 0x0003)          # Major Version 3
    struct.pack_into('<H', header, 28, 0xFFFE)          # Byte Order: Little-Endian
    struct.pack_into('<H', header, 30, 9)               # Sector Shift: 512
    struct.pack_into('<H', header, 32, 6)               # Mini Sector Shift: 64
    struct.pack_into('<I', header, 44, num_fat)         # Number of FAT Sectors
    struct.pack_into('<I', header, 48, dir_sector)      # First Directory Sector
    struct.pack_into('<I', header, 56, 4096)            # Mini Stream Cutoff Size
    struct.pack_into('<I', header, 60, _ENDOFCHAIN)     # First Mini FAT Sector
    struct.pack_into('<I', header, 64, 0)               # Number of Mini FAT Sectors
    struct.pack_into('<I', header, 68, _ENDOFCHAIN)     # First DIFAT Sector
    struct.pack_into('<I', header, 72, 0)               # Number of DIFAT Sectors
    struct.pack_into('<109I', header, 76, *difat)

    # 2. Directory Sector（Entry 0 = Root Entry，Entry 1 = Workbook）
    def make_dir_entry(name: str, obj_type: int, child: int, start_sec: int, size: int) -> bytes:
        entry = bytearray(128)
        encoded_name = (name + '\x00').encode('utf-16-le')
        entry[:len(encoded_name)] = encoded_name
        struct.pack_into('<H', entry, 64, len(encoded_name))
        entry[66] = obj_type
        entry[67] = 1  # Black node
        struct.pack_into('<III', entry, 68, _FREESECT, _FREESECT, child)
        struct.pack_into('<II', entry, 116, start_sec, size)
        return bytes(entry)

    dir_sector_bytes = (
        make_dir_entry('Root Entry', 5, 1, _ENDOFCHAIN, 0)
        + make_dir_entry('Workbook', 2, _FREESECT, 0, raw_len)
        + bytes(256)
    )

    fat_bytes = struct.pack(f'<{len(fat_entries)}I', *fat_entries)
    return bytes(header) + padded_stream + dir_sector_bytes + fat_bytes


def write_xls_sheets(sheets: list[dict]) -> bytes:
    """将 [{'name': str, 'rows': list[list]}] 写出为标准二进制 .xls 字节串。"""
    if not sheets:
        sheets = [{'name': 'Sheet1', 'rows': []}]

    try:
        import xlwt  # type: ignore[import-not-found]
    except ImportError:
        xlwt = None
    if xlwt is not None:
        wb = xlwt.Workbook(encoding='utf-8')
        for item in sheets:
            ws = wb.add_sheet(str(item.get('name') or 'Sheet1')[:31])
            for r_idx, row in enumerate(item.get('rows') or []):
                for c_idx, val in enumerate(row):
                    if val is not None:
                        ws.write(r_idx, c_idx, val)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # 1. 收集共享字符串表 SST
    sst_index: dict[str, int] = {}
    sst_list: list[str] = []
    total_str_refs = 0
    for sheet in sheets:
        for row in sheet.get('rows') or []:
            for val in row:
                if val is None or isinstance(val, (bool, int, float)):
                    continue
                text = str(val)
                if text not in sst_index:
                    sst_index[text] = len(sst_list)
                    sst_list.append(text)
                total_str_refs += 1

    # 2. 生成各个 Sheet 的 BIFF8 流
    sheet_streams: list[bytes] = []
    for sheet in sheets:
        rows = sheet.get('rows') or []
        out = bytearray()
        # Worksheet BOF (BIFF8, type=0x0010)
        out.extend(_biff_record(0x0809, struct.pack('<HHHHII', 0x0600, 0x0010, 0x0DBB, 0x07CC, 0, 0x0006)))
        max_rows = len(rows)
        max_cols = max((len(r) for r in rows), default=0)
        out.extend(_biff_record(0x0200, struct.pack('<IIHHH', 0, max_rows, 0, max_cols, 0)))
        for r_idx, row in enumerate(rows):
            if r_idx >= 65536:
                break
            for c_idx, val in enumerate(row):
                if c_idx >= 256 or val is None:
                    continue
                if isinstance(val, bool):
                    out.extend(_biff_record(0x0205, struct.pack('<HHHBB', r_idx, c_idx, 15, 1 if val else 0, 0)))
                elif isinstance(val, (int, float)) and math.isfinite(float(val)):
                    out.extend(_biff_record(0x0203, struct.pack('<HHHd', r_idx, c_idx, 15, float(val))))
                else:
                    s_idx = sst_index[str(val)]
                    out.extend(_biff_record(0x00FD, struct.pack('<HHHI', r_idx, c_idx, 15, s_idx)))
        out.extend(_biff_record(0x000A, b''))  # EOF
        sheet_streams.append(bytes(out))

    # 3. 构造全局工作簿头（BOF + CODEPAGE + WINDOW1 + 最小 FONT/XF + BOUNDSHEET + SST + EOF）
    prefix = bytearray()
    prefix.extend(_biff_record(0x0809, struct.pack('<HHHHII', 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 0x0006)))
    prefix.extend(_biff_record(0x0042, struct.pack('<H', 1200)))  # UTF-16LE
    prefix.extend(_biff_record(0x003D, struct.pack('<HHHHHHHHH', 0x00E0, 0x005A, 0x3F00, 0x2400, 0x0038, 0, 0, 1, 0x0258)))

    # 5 个默认 FONT 与 16 个默认 XF（第 15 号为标准单元格样式）
    font_name = 'Arial'
    font_payload = struct.pack('<HHHHHBBBBB', 200, 0, 0x7FFF, 400, 0, 0, 0, 0, 0, len(font_name)) + b'\x00' + font_name.encode('latin-1')
    for _ in range(5):
        prefix.extend(_biff_record(0x0031, font_payload))
    for i in range(16):
        xf_type = 0xFFF5 if i < 15 else 0x0001
        prefix.extend(_biff_record(0x00E0, struct.pack('<HHHHIHHHH', 0, 0, xf_type, 0x0020, 0, 0, 0, 0, 0x20C0)))

    # 预计算 BOUNDSHEET 区块与 SST 区块长度，精确回填每个 Sheet 的 bof_offset
    bs_names = [_encode_biff8_short_utf16(str(s.get('name') or f'Sheet{i + 1}')) for i, s in enumerate(sheets)]
    bs_total_len = sum(4 + 6 + len(raw_name) for raw_name in bs_names)
    sst_bytes = _build_sst_records(sst_list, total_str_refs) if sst_list else b''
    eof_bytes = _biff_record(0x000A, b'')

    sheet_offset = len(prefix) + bs_total_len + len(sst_bytes) + len(eof_bytes)
    boundsheets = bytearray()
    for raw_name, s_stream in zip(bs_names, sheet_streams):
        boundsheets.extend(_biff_record(0x0085, struct.pack('<IBB', sheet_offset, 0x00, 0x00) + raw_name))
        sheet_offset += len(s_stream)

    workbook_stream = bytes(prefix) + bytes(boundsheets) + sst_bytes + eof_bytes + b''.join(sheet_streams)
    return _wrap_ole2_workbook(workbook_stream)
