"""Bounded E-file events; rows are consumed without accumulating modules."""
import re

from .definition_files import tokens
from .table_schema import SchemaError

MAX_LINE = 1024 * 1024
MAX_FIELDS = 512


def logical_lines(stream):
    pieces, quote, start, size, previous = [], None, 1, 0, ''
    number = 0
    while raw := stream.readline(MAX_LINE + 1):
        number += 1
        if not pieces:
            start = number
        size += len(raw)
        if size > MAX_LINE:
            raise SchemaError(f'第 {start} 行：单条逻辑行超过1 Mi字符')
        i = 0
        while i < len(raw):
            ch = raw[i]
            if quote and ch == quote:
                if i + 1 < len(raw) and raw[i + 1] == quote:
                    previous = ch; i += 2; continue
                quote = None
            elif not quote and ch in "\"'" and (not previous or previous.isspace() or previous in '=@#'):
                quote = ch
            previous = ch; i += 1
        pieces.append(raw)
        if not quote:
            yield start, ''.join(pieces).strip()
            pieces, size, previous = [], 0, ''
    if quote:
        raise SchemaError(f'第 {start} 行：引号未闭合')


def events(stream):
    current, seen = None, set()
    for number, line in logical_lines(stream):
        if not line or line.startswith('<!') or (line.startswith('//') and current is None):
            continue
        if line.startswith('</'):
            if current is None or not line.endswith('>') or line[2:-1].strip() != current['tag']:
                raise SchemaError(f'第 {number} 行：结束标签不匹配')
            if not current['columns']:
                raise SchemaError(f"{current['name']} 缺少 @ 字段定义")
            yield 'end', number, current
            current = None
        elif line.startswith('<') and line.endswith('>'):
            if current:
                raise SchemaError(f'第 {number} 行：不支持嵌套模块')
            body = line[1:-1].strip()
            if not body:
                raise SchemaError(f'第 {number} 行：模块名为空')
            tag = body.split()[0]; name = tag.split('::')[0]
            if name.casefold() in seen:
                raise SchemaError(f'重复模块：{name}')
            seen.add(name.casefold())
            if len(seen) > 200:
                raise SchemaError('一次最多导入200张表')
            props = {m[1]: (m[2] or m[3] or m[4]) for m in re.finditer(
                r'''(\w+)\s*=\s*(?:'([^']*)'|"([^"]*)"|([^\s]+))''', body)}
            current = dict(name=name, tag=tag, props=props, columns=[], descriptions=[])
            yield 'start', number, current
        elif current and line.startswith('@'):
            if current['columns']:
                raise SchemaError(f'第 {number} 行：重复 @ 字段定义')
            current['columns'] = tokens(line[1:])
            if not 1 <= len(current['columns']) <= MAX_FIELDS or len(set(current['columns'])) != len(current['columns']):
                raise SchemaError(f'第 {number} 行：字段重复或字段数不在1–512范围')
            yield 'columns', number, current
        elif current and line.startswith('//'):
            current['descriptions'] = tokens(line[2:])
        elif current and line.startswith('#'):
            cells = tokens(line[1:])
            if not current['columns'] or len(cells) != len(current['columns']):
                raise SchemaError(f"{current['name']} 第 {number} 行：值数量与字段不一致；空字符串请写 ''")
            if any(len(cell) > 131072 for cell in cells):
                raise SchemaError(f'第 {number} 行：单字段超过128 Ki字符')
            yield 'row', number, cells
        else:
            raise SchemaError(f'第 {number} 行：无法识别的 E 文件内容')
    if current:
        raise SchemaError(f"{current['name']} 缺少结束标签")
    if not seen:
        raise SchemaError('文件中未找到数据模块')
