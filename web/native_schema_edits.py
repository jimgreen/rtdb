"""Update application-owned SQLite layouts in place without replacing tables."""

from contextlib import closing, nullcontext
from dataclasses import dataclass
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
    if desired.column_names != (*retained, *(col.eng_name for col in added)):
        raise APIError(422, '原生表保留已有字段顺序，新字段应追加在末尾')
    if tuple(renames.get(key, key) for key in old.primary_keys) != desired.primary_keys:
        raise APIError(422, '原生表不能在此修改主键组合')
    if old.indexes is None:
        retained_indexes = None
    else:
        retained_indexes = tuple(tuple(renames.get(field, field) for field in fields) for fields in old.indexes)
    if retained_indexes != desired.indexes:
        raise APIError(422, '原生表保留已有索引，不能在此修改索引方案')
    for field in retained:
        before, after = previous[field], current[field]
        if before.data_type != after.data_type or before.default_value != after.default_value:
            raise APIError(422, f'原生表的已有字段 {field} 不支持在此改类型或默认值')
    generated = {row[1] for row in connection.execute(f'PRAGMA table_xinfo({_quote(name)})') if row[6] != 0}
    if generated.intersection((*removed, *renames)):
        raise APIError(422, '原生表的生成字段不能删除或改名')
    if removed:
        for _, index, *_ in connection.execute(f'PRAGMA index_list({_quote(name)})'):
            indexed = {row[2] for row in connection.execute(f'PRAGMA index_info({_quote(index)})')}
            if indexed.intersection(removed):
                raise APIError(422, '已有索引引用待删除字段，不能删除')

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
    ordinal = connection.execute(
        f'SELECT ordinal FROM {_quote(EMBEDDED_TABLES_TABLE)} WHERE table_name=?', (name,),
    ).fetchone()[0]
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


def save_native_schema(manager, name, payload, *, creating=False, already_locked=False):
    from .sqlite_service import fingerprint

    path = _path(manager)

    def apply(connection):
        plan = _plan(connection, manager, path, name, payload, creating=creating)
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
