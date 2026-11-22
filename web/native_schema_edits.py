"""Update application-owned SQLite layouts in place without replacing tables."""

from contextlib import closing, nullcontext
from dataclasses import dataclass, field
import math
import re
import shutil
import sqlite3
import uuid

from core import TableDefinition
from core.definition_files import normalize_definition_labels
from core.sqlite_db import _BLOB, _BOOL, _FLOAT, _INT, _TEXT
from core.sqlite_schema import (
    EMBEDDED_TABLES_TABLE, _read_embedded_database, remove_embedded_definition,
    validate_native_table, write_native_definition,
)

from . import write_receipts
from .errors import APIError


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _column_sql(connection, column):
    kind = column.data_type.lower()
    if kind in _INT | _BOOL:
        declared = 'INTEGER'
    elif kind in _FLOAT:
        declared = 'REAL'
    elif kind in _TEXT:
        declared = 'TEXT'
    elif kind in _BLOB:
        declared = 'BLOB'
    else:
        raise APIError(422, f'原生 SQLite 字段 {column.eng_name} 不支持类型 {column.data_type}')
    sql = f'{_quote(column.eng_name)} {declared}'
    if column.primary_key:
        sql += ' NOT NULL'
    default = column.default_value
    if default is not None:
        if isinstance(default, str) and '\x00' in default:
            raise APIError(422, f'字段 {column.eng_name} 的默认值不能包含空字符')
        literal = connection.execute('SELECT quote(?)', (default,)).fetchone()[0]
        sql += f' DEFAULT {literal}'
    return sql


def _document(connection, path):
    document = _read_embedded_database(connection, path)
    if document is None or document.storage_format != 'native':
        raise APIError(409, '原生数据库内置结构已变化，请重新打开数据库')
    return document


def _definition(manager, payload):
    raw = normalize_definition_labels(payload.get('definition'))
    head = raw.get('head') if isinstance(raw, dict) else None
    name = head.get('eng_name') if isinstance(head, dict) else None
    name = name.strip() if isinstance(name, str) else None
    definition = TableDefinition.from_mapping(raw, manager.schema_dir, native_table=name)
    if definition.eng_name.lower().startswith(('sqlite_', 'rtdb_')):
        raise APIError(400, '该表名由 SQLite 或 RTDB 保留')
    if len({column.lower() for column in definition.column_names}) != len(definition.columns):
        raise APIError(422, '原生 SQLite 字段名不区分英文大小写，名称不能重复')
    return definition


def _existing(connection, document, name, version):
    from .sqlite_service import fingerprint

    old = next((table for table in document.tables if table.eng_name == name), None)
    if old is None:
        raise APIError(404, f'找不到原生表：{name}')
    if version != fingerprint(old.to_mapping()):
        raise APIError(409, '表结构已变化，请刷新后修改')
    validate_native_table(connection, old)
    return old


def _ordinary_table(connection, name):
    # PRAGMA table_list distinguishes real tables from virtual/shadow tables.
    for schema, table, kind, *_ in connection.execute('PRAGMA main.table_list'):
        if schema == 'main' and table == name:
            if kind == 'table':
                return
            break
    raise APIError(422, '此 SQLite 表类型不支持原位修改结构')


@dataclass
class _Plan:
    definition: TableDefinition
    statements: list[str]
    changes: list[str]
    ordinal: int | None = None
    rebuild: bool = False
    rebuild_rows: list[tuple] = field(default_factory=list)
    without_rowid: bool = False
    rebuild_indexes: list[str] = field(default_factory=list)
    rebuild_triggers: list[str] = field(default_factory=list)


def _convert_native_value(value, column):
    """将原生 SQLite 已有单元值无损转换并校验为目标列类型。"""
    name = column.eng_name
    kind = column.data_type.lower()
    if value is None:
        if column.primary_key:
            raise APIError(422, f'主键字段 {name} 包含空值，不能作为主键')
        return None
    if kind in _INT:
        if isinstance(value, bool):
            result = int(value)
        elif isinstance(value, int):
            result = value
        elif isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                raise APIError(422, f'字段 {name} 包含非整数值 {value!r}，无法转换为整数')
            result = int(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                if column.primary_key:
                    raise APIError(422, f'主键字段 {name} 包含空字符串，无法转换为整数')
                return None
            if not re.fullmatch(r'-?\d+', text):
                raise APIError(422, f'字段 {name} 包含无法转为整数的值 {value!r}')
            result = int(text)
        else:
            raise APIError(422, f'字段 {name} 的二进制或对象值无法转换为整数')
        if result < -(1 << 63) or result >= (1 << 63):
            raise APIError(422, f'字段 {name} 的整数值超出 64 位范围')
        return result
    if kind in _FLOAT:
        if isinstance(value, bool):
            result = float(int(value))
        elif isinstance(value, (int, float)):
            result = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                if column.primary_key:
                    raise APIError(422, f'主键字段 {name} 包含空字符串，无法转换为浮点数')
                return None
            try:
                result = float(text)
            except ValueError as error:
                raise APIError(422, f'字段 {name} 包含无法转为浮点数的值 {value!r}') from error
        else:
            raise APIError(422, f'字段 {name} 的值无法转换为浮点数')
        if not math.isfinite(result):
            raise APIError(422, f'字段 {name} 包含非法浮点值 {value!r}')
        return result
    if kind in _BOOL:
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)):
            if value in (0, 1):
                return int(value)
            raise APIError(422, f'字段 {name} 包含非布尔数值 {value!r}，无法转换为布尔')
        if isinstance(value, str):
            text = value.strip().lower()
            if not text:
                if column.primary_key:
                    raise APIError(422, f'主键字段 {name} 包含空字符串，无法转换为布尔')
                return None
            if text in ('true', '1'):
                return 1
            if text in ('false', '0'):
                return 0
            raise APIError(422, f'字段 {name} 包含无法转为布尔的值 {value!r}')
        raise APIError(422, f'字段 {name} 的值无法转换为布尔')
    if kind in _TEXT:
        if isinstance(value, bytes):
            try:
                return value.decode('utf-8')
            except UnicodeDecodeError as error:
                raise APIError(422, f'字段 {name} 包含非 UTF-8 二进制数据，无法转换为文本') from error
        if isinstance(value, bool):
            return 'true' if value else 'false'
        return str(value)
    if kind in _BLOB:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                if column.primary_key:
                    raise APIError(422, f'主键字段 {name} 包含空字符串，无法转换为 BLOB')
                return None
            try:
                return bytes.fromhex(text)
            except ValueError as error:
                raise APIError(422, f'字段 {name} 的文本不是有效十六进制，无法转换为 BLOB') from error
        raise APIError(422, f'字段 {name} 的值无法转换为 BLOB')
    raise APIError(422, f'原生 SQLite 字段 {name} 不支持类型 {column.data_type}')


def _strip_sql_literals(sql):
    """剥离 SQL 字符串与注释内容，便于安全检测表级约束关键字。"""
    cleaned = re.sub(r'--[^\n]*', ' ', sql or '')
    cleaned = re.sub(r'/\*.*?\*/', ' ', cleaned, flags=re.S)
    cleaned = re.sub(r"'(?:''|[^'])*'", "''", cleaned)
    cleaned = re.sub(r'"(?:""|[^"])*"', '""', cleaned)
    return cleaned


def _check_rebuild_constraints(connection, table_name, table_sql):
    """重建原生表前拦截会导致静默丢失的高危表级约束，并返回需要告知用户的提示。"""
    cleaned = _strip_sql_literals(table_sql)
    if re.search(r'\bSTRICT\b', cleaned, re.I):
        raise APIError(422, '原生表启用了 STRICT 模式，暂不支持通过重建修改结构')
    if re.search(r'\bAUTOINCREMENT\b', cleaned, re.I):
        raise APIError(422, '原生表包含 AUTOINCREMENT 约束，暂不支持通过重建修改结构')
    if re.search(r'\b(?:FOREIGN\s+KEY|REFERENCES)\b', cleaned, re.I) or connection.execute(
        f'PRAGMA foreign_key_list({_quote(table_name)})'
    ).fetchall():
        raise APIError(422, '原生表包含外键（FOREIGN KEY）约束，为避免丢失约束暂不支持重建表结构')
    for _seq, _idx_name, unique, origin, _partial in connection.execute(f'PRAGMA index_list({_quote(table_name)})'):
        if unique and origin == 'u':
            raise APIError(422, '原生表包含非主键 UNIQUE 约束，为避免丢失约束暂不支持重建表结构')
    notes = []
    if re.search(r'\bCHECK\s*\(', cleaned, re.I):
        notes.append('注意：原表包含 CHECK 约束，重建后将不再保留原 CHECK 表达式')
    return notes


def _collect_rebuild_objects(connection, old_name, new_name, renames, desired_names):
    """收集原表需要保留的显式二级索引与触发器 DDL。"""
    index_meta = {}
    for _seq, idx_name, unique, origin, partial in connection.execute(f'PRAGMA index_list({_quote(old_name)})'):
        if origin != 'c':
            continue
        cols = [row[2] for row in connection.execute(f'PRAGMA index_info({_quote(idx_name)})')]
        index_meta[idx_name] = (bool(unique), bool(partial), cols)

    indexes = []
    for idx_name, sql in connection.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='index' AND tbl_name=? COLLATE NOCASE AND sql IS NOT NULL",
        (old_name,),
    ):
        meta = index_meta.get(idx_name)
        if meta is not None and not meta[1] and all(col is not None for col in meta[2]):
            unique, _partial, cols = meta
            mapped = [renames.get(col, col) for col in cols]
            if all(col in desired_names for col in mapped):
                prefix = 'CREATE UNIQUE INDEX' if unique else 'CREATE INDEX'
                cols_sql = ', '.join(_quote(col) for col in mapped)
                indexes.append(f'{prefix} {_quote(idx_name)} ON {_quote(new_name)} ({cols_sql})')
        elif old_name == new_name and not renames:
            indexes.append(sql)
        else:
            raise APIError(422, f'原生表包含部分索引或表达式索引（{idx_name}），无法在改名或重建时安全保留')

    triggers = [
        sql for (sql,) in connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=? COLLATE NOCASE AND sql IS NOT NULL",
            (old_name,),
        )
    ]
    if triggers and (old_name != new_name or renames):
        raise APIError(422, '当前表包含触发器，不能在重建表结构的同时修改表名或字段名，请分步操作')
    return indexes, triggers


def _prepare_rebuild_rows(connection, old, desired, renames):
    """按目标结构逐行转换现有数据，并校验目标主键非空且唯一。"""
    reverse_renames = {after: before for before, after in renames.items()}
    old_names = set(old.column_names)
    select_cols = [col.eng_name for col in old.columns]
    select_sql = ', '.join(_quote(col) for col in select_cols)
    source_index = {col: idx for idx, col in enumerate(select_cols)}

    # 预先构建每列的数据来源（原列索引或新列默认值）
    sources = []
    for col in desired.columns:
        old_col = reverse_renames.get(col.eng_name, col.eng_name)
        if old_col in old_names:
            sources.append((True, source_index[old_col], col))
        else:
            default_val = _convert_native_value(col.default_value, col)
            sources.append((False, default_val, col))

    pk_positions = [idx for idx, col in enumerate(desired.columns) if col.primary_key]
    seen_pks = set()
    converted_rows = []
    for raw_row in connection.execute(f'SELECT {select_sql} FROM {_quote(old.eng_name)}'):
        out = []
        for from_old, src, col in sources:
            value = _convert_native_value(raw_row[src], col) if from_old else src
            out.append(value)
        pk_key = tuple(out[pos] for pos in pk_positions)
        if any(part is None for part in pk_key):
            raise APIError(422, '目标主键包含空值，无法修改主键')
        if pk_key in seen_pks:
            raise APIError(422, f'按新主键存在重复记录：{pk_key!r}，请先去重后再修改主键')
        seen_pks.add(pk_key)
        converted_rows.append(tuple(out))
    return converted_rows


def _plan(connection, manager, path, name, payload, *, creating):
    from .sqlite_service import _normalise_column_renames

    document = _document(connection, path)
    desired = _definition(manager, payload)
    new_name = desired.eng_name
    target = connection.execute(
        'SELECT name FROM sqlite_schema WHERE name=? COLLATE NOCASE AND type IN (\'table\',\'view\',\'index\')',
        (new_name,),
    ).fetchone()
    if target and (creating or target[0] != name):
        raise APIError(409, f'目标表名已存在：{new_name}')

    quoted_table = _quote(new_name)
    if creating:
        columns = [_column_sql(connection, col) for col in desired.columns]
        keys = ', '.join(_quote(key) for key in desired.primary_keys)
        columns.append(f'PRIMARY KEY ({keys})')
        statements = [f'CREATE TABLE {quoted_table} ({", ".join(columns)})']
        for fields in desired.secondary_indexes:
            if fields != desired.primary_keys:
                index = _quote('rtdb_native_idx_' + uuid.uuid4().hex)
                columns_sql = ', '.join(_quote(field) for field in fields)
                statements.append(f'CREATE INDEX {index} ON {quoted_table} ({columns_sql})')
        return _Plan(desired, statements, [f'新建原生表：{new_name}'])

    old = _existing(connection, document, name, payload.get('version'))
    _ordinary_table(connection, name)
    renames = _normalise_column_renames(old, desired, payload.get('column_renames'))
    previous = {renames.get(col.eng_name, col.eng_name): col for col in old.columns}
    current = {col.eng_name: col for col in desired.columns}
    retained = [target_name for target_name in previous if target_name in current]
    removed = [col.eng_name for col in old.columns if renames.get(col.eng_name, col.eng_name) not in current]
    added = [col for col in desired.columns if col.eng_name not in previous]
    if old.indexes is None:
        retained_indexes = None
    else:
        retained_indexes = tuple(tuple(renames.get(field, field) for field in fields) for fields in old.indexes)
    if retained_indexes != desired.indexes:
        raise APIError(422, '原生表保留已有索引，不能在此修改索引方案')
    generated = {row[1] for row in connection.execute(f'PRAGMA table_xinfo({_quote(name)})') if row[6] != 0}
    if generated.intersection((*removed, *renames)):
        raise APIError(422, '原生表的生成字段不能删除或改名')
    if removed:
        for _, index, *_ in connection.execute(f'PRAGMA index_list({_quote(name)})'):
            indexed = {row[2] for row in connection.execute(f'PRAGMA index_info({_quote(index)})')}
            if indexed.intersection(removed):
                raise APIError(422, '已有索引引用待删除字段，不能删除')

    order_changed = desired.column_names != (*retained, *(col.eng_name for col in added))
    pk_changed = tuple(renames.get(key, key) for key in old.primary_keys) != desired.primary_keys
    type_changes = [
        f for f in retained if previous[f].data_type != current[f].data_type
    ]
    default_changes = [
        f for f in retained if previous[f].default_value != current[f].default_value
    ]
    needs_rebuild = order_changed or pk_changed or bool(type_changes) or bool(default_changes)

    ordinal = connection.execute(
        f'SELECT ordinal FROM {_quote(EMBEDDED_TABLES_TABLE)} WHERE table_name=?', (name,),
    ).fetchone()[0]

    if needs_rebuild:
        if generated:
            raise APIError(422, '原生表包含 SQLite 生成列，暂不支持通过重建修改类型、主键或默认值')
        changes = []
        if name != new_name:
            changes.append(f'表改名：{name} → {new_name}')
        for field in removed:
            changes.append(f'删除字段及其数据：{field}')
        for before, after in renames.items():
            changes.append(f'字段改名：{before} → {after}')
        for column in added:
            changes.append(f'增加字段：{column.eng_name}')
        for field in type_changes:
            changes.append(f'修改字段类型：{field}（{previous[field].data_type} → {current[field].data_type}）')
        for field in default_changes:
            changes.append(f'修改字段默认值：{field}')
        if pk_changed:
            changes.append(f'修改主键：{", ".join(old.primary_keys)} → {", ".join(desired.primary_keys)}')
        if order_changed:
            changes.append('调整字段顺序')
        if old.chn_name != desired.chn_name:
            changes.append(f'表显示名：{old.chn_name} → {desired.chn_name}')
        for field in retained:
            before, after = previous[field], current[field]
            if (before.chn_name, before.disp_type, before.disp_menu) != (after.chn_name, after.disp_type, after.disp_menu):
                changes.append(f'字段显示属性：{field}')
        changes.append('重建原生表并保留全部数据与索引')

        table_sql_row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=? COLLATE NOCASE", (name,),
        ).fetchone()
        table_sql = table_sql_row[0] if table_sql_row and table_sql_row[0] else ''
        changes.extend(_check_rebuild_constraints(connection, name, table_sql))
        without_rowid = bool(re.search(r'\bWITHOUT\s+ROWID\b', table_sql, re.I))
        rebuild_indexes, rebuild_triggers = _collect_rebuild_objects(
            connection, name, new_name, renames, set(desired.column_names),
        )
        rebuild_rows = _prepare_rebuild_rows(connection, old, desired, renames)
        return _Plan(
            desired, [], changes, ordinal,
            rebuild=True, rebuild_rows=rebuild_rows, without_rowid=without_rowid,
            rebuild_indexes=rebuild_indexes, rebuild_triggers=rebuild_triggers,
        )

    statements, changes = [], []
    if name != new_name:
        if name.lower() == new_name.lower():
            temporary = _quote('rtdb_native_rename_' + uuid.uuid4().hex)
            statements.append(f'ALTER TABLE {_quote(name)} RENAME TO {temporary}')
            statements.append(f'ALTER TABLE {temporary} RENAME TO {quoted_table}')
        else:
            statements.append(f'ALTER TABLE {_quote(name)} RENAME TO {quoted_table}')
        changes.append(f'表改名：{name} → {new_name}')
    for field in removed:
        statements.append(f'ALTER TABLE {quoted_table} DROP COLUMN {_quote(field)}')
        changes.append(f'删除字段及其数据：{field}')
    # Temporary names permit swaps and case-only changes without losing data.
    staged = []
    for before, after in renames.items():
        temporary = _quote('rtdb_native_rename_' + uuid.uuid4().hex)
        statements.append(f'ALTER TABLE {quoted_table} RENAME COLUMN {_quote(before)} TO {temporary}')
        staged.append((temporary, after))
        changes.append(f'字段改名：{before} → {after}')
    for temporary, after in staged:
        statements.append(f'ALTER TABLE {quoted_table} RENAME COLUMN {temporary} TO {_quote(after)}')
    for column in added:
        statements.append(f'ALTER TABLE {quoted_table} ADD COLUMN {_column_sql(connection, column)}')
        changes.append(f'增加字段：{column.eng_name}')
    if old.chn_name != desired.chn_name:
        changes.append(f'表显示名：{old.chn_name} → {desired.chn_name}')
    for field in retained:
        before, after = previous[field], current[field]
        if (before.chn_name, before.disp_type, before.disp_menu) != (after.chn_name, after.disp_type, after.disp_menu):
            changes.append(f'字段显示属性：{field}')
    return _Plan(desired, statements, changes, ordinal)


def _path(manager):
    path = manager.data_dir / manager.database_file
    if path.parent != manager.data_dir or path.resolve() != path or not path.is_file():
        raise APIError(404, '找不到原生数据库文件')
    return path


def _run_write(manager, path, callback, *, already_locked=False):
    manager.require_writable()
    gate = nullcontext() if already_locked else manager.exclusive()
    try:
        with gate, closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)) as connection:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA legacy_alter_table=OFF')
            connection.execute('BEGIN IMMEDIATE')
            with connection:
                result = write_receipts.in_transaction(connection, callback)
            write_receipts.mark_committed(result)
            return result
    except sqlite3.Error as error:
        code = getattr(error, 'sqlite_errorcode', 0) or 0
        if code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise APIError(409, '原生 SQLite 数据库正在使用，请稍后重试') from error
        raise APIError(422, f'无法修改原生表结构：{error}') from error


def _apply_native_rebuild(connection, old_name, plan):
    """在当前事务内原子重建原生 SQLite 表并恢复数据、二级索引与触发器。"""
    desired = plan.definition
    temp_name = 'rtdb_native_rebuild_' + uuid.uuid4().hex
    quoted_temp = _quote(temp_name)
    quoted_old = _quote(old_name)
    quoted_new = _quote(desired.eng_name)
    columns = [_column_sql(connection, col) for col in desired.columns]
    keys = ', '.join(_quote(key) for key in desired.primary_keys)
    columns.append(f'PRIMARY KEY ({keys})')
    suffix = ' WITHOUT ROWID' if plan.without_rowid else ''

    # 推迟外键校验到事务提交前，确保 DROP/RENAME 期间外键关系安全切换并最终完整校验。
    connection.execute('PRAGMA defer_foreign_keys = ON')
    connection.execute(f'CREATE TABLE {quoted_temp} ({", ".join(columns)}){suffix}')
    if plan.rebuild_rows:
        placeholders = ', '.join('?' for _ in desired.columns)
        col_list = ', '.join(_quote(col.eng_name) for col in desired.columns)
        connection.executemany(
            f'INSERT INTO {quoted_temp} ({col_list}) VALUES ({placeholders})',
            plan.rebuild_rows,
        )
    connection.execute(f'DROP TABLE {quoted_old}')
    connection.execute(f'ALTER TABLE {quoted_temp} RENAME TO {quoted_new}')
    for index_sql in plan.rebuild_indexes:
        connection.execute(index_sql)
    for trigger_sql in plan.rebuild_triggers:
        connection.execute(trigger_sql)
    fk_violations = connection.execute('PRAGMA foreign_key_check').fetchall()
    if fk_violations:
        raise APIError(422, f'修改表结构破坏了外键约束：{fk_violations[0]}')


def save_native_schema(manager, name, payload, *, creating=False, already_locked=False):
    from .sqlite_service import fingerprint

    path = _path(manager)

    def apply(connection):
        plan = _plan(connection, manager, path, name, payload, creating=creating)
        if plan.rebuild:
            _apply_native_rebuild(connection, name, plan)
        else:
            for sql in plan.statements:
                connection.execute(sql)
        desired = plan.definition
        validate_native_table(connection, desired)
        if not creating and desired.eng_name != name:
            remove_embedded_definition(connection, name)
        write_native_definition(connection, desired, plan.ordinal)
        mapping = desired.to_mapping()
        return dict(name=desired.eng_name, definition=mapping, schema_version=fingerprint(mapping))

    return _run_write(manager, path, apply, already_locked=already_locked)


def preview_native_schema(manager, name, payload):
    path = _path(manager)
    creating = bool(payload.get('creating'))
    with manager.read_scope(), closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        connection.execute('BEGIN')
        try:
            plan = _plan(connection, manager, path, name, payload, creating=creating)
        except APIError as error:
            if error.status != 422:
                raise
            return dict(changes=[], errors=[error.message], row_count=0, valid=False)
        count = 0 if creating else connection.execute(f'SELECT COUNT(*) FROM {_quote(name)}').fetchone()[0]
        return dict(changes=plan.changes, errors=[], row_count=count, valid=True)


def _check_drop_references(connection, name):
    def objects():
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE type IN ('table','trigger','view') "
            'AND tbl_name<>? COLLATE NOCASE ORDER BY name', (name,),
        ).fetchall()

    # SQLite rewrites referencing foreign keys, triggers and views on table
    # rename. Compare those definitions with a rolled-back rename so that DROP
    # cannot leave a dangling dependency in another object.
    before = objects()
    temporary = 'rtdb_native_drop_probe_' + uuid.uuid4().hex
    connection.execute('SAVEPOINT native_drop_probe')
    try:
        connection.execute(f'ALTER TABLE {_quote(name)} RENAME TO {_quote(temporary)}')
        after = {(kind, other): sql for kind, other, sql in objects()}
    finally:
        connection.execute('ROLLBACK TO native_drop_probe')
        connection.execute('RELEASE native_drop_probe')
    if any(after.get((kind, other)) != sql for kind, other, sql in before):
        raise APIError(409, '其他表、视图或触发器引用此表，不能删除')


def drop_native_schema(manager, name, payload):
    if payload.get('confirm') != name:
        raise APIError(400, '请输入表名确认删除')
    path = _path(manager)
    manager.require_writable()
    with manager.exclusive():
        # 先在事务外验证版本与空表状态并完成备份，避免在写事务内对活跃连接执行 backup 导致死锁或自锁。
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as source:
            document = _document(source, path)
            _existing(source, document, name, payload.get('version'))
            _ordinary_table(source, name)
            if source.execute(f'SELECT 1 FROM {_quote(name)} LIMIT 1').fetchone():
                raise APIError(409, '表有数据，请先导出并清空后删除表结构')
            safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', name)[:48] or 'table'
            archive = manager.data_dir / '.web-archive' / f'{safe_name}-{uuid.uuid4().hex}'
            archive.mkdir(parents=True)
            try:
                with closing(sqlite3.connect(archive / path.name, timeout=5)) as backup:
                    source.backup(backup)
            except BaseException:
                shutil.rmtree(archive, ignore_errors=True)
                raise

        def apply(connection):
            document = _document(connection, path)
            _existing(connection, document, name, payload.get('version'))
            _ordinary_table(connection, name)
            if connection.execute(f'SELECT 1 FROM {_quote(name)} LIMIT 1').fetchone():
                raise APIError(409, '表有数据，请先导出并清空后删除表结构')
            _check_drop_references(connection, name)
            connection.execute(f'DROP TABLE {_quote(name)}')
            remove_embedded_definition(connection, name)
            return dict(deleted=True, archive=str(archive), database_file=path.name,
                        remaining_tables=[table.eng_name for table in document.tables if table.eng_name != name])

        try:
            return _run_write(manager, path, apply, already_locked=True)
        except BaseException:
            shutil.rmtree(archive, ignore_errors=True)
            raise
