"""SQLite management operations. Each request owns and closes its connections.

The public row API uses logical names and primary keys, never grid positions.
SQLite embedded schema tables are authoritative at runtime. Schema changes use
a recovery journal and regenerate JSON/Python export files when available.
"""
from __future__ import annotations

import csv
import base64
import copy
import hashlib
import itertools
import io
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import tempfile
import time
import uuid
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from core import SQLiteDatabase, TableDefinition, SchemaError, SchemaRegistry, load_database_definition
from core.file_mapped_db import _locked
from core.sqlite_db import _INT, _FLOAT, _BOOL, _TEXT, _OBJECT
from core.sqlite_schema import (ensure_embedded_schema, find_embedded_table, load_embedded_database,
                                remove_embedded_definition, write_embedded_definition)
from core.definition_files import python_definition, normalize_definition_labels
from .audit import OperationAuditLog, error_summary, result_summary, status_for_exception
from .errors import APIError


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def browser_value(value):
    """Keep signed 64-bit identifiers exact in JavaScript."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2 ** 53 - 1:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: browser_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [browser_value(v) for v in value]
    return value


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def atomic_text(path, text):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sqlite_fingerprint(path):
    if not path.exists():
        return None
    with closing(sqlite3.connect(path)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
            return None
        found = connection.execute('SELECT fingerprint FROM metadata WHERE version=1').fetchone()
        return found[0] if found else None


def _sqlite_table_fingerprint(path, table_name=None):
    """Read one table's stored schema fingerprint from legacy or grouped DBs."""
    if table_name is None:
        return _sqlite_fingerprint(path)
    if not path.exists():
        return None
    metadata = 'rtdb_metadata_' + table_name
    quote = lambda value: '"' + value.replace('"', '""') + '"'
    with closing(sqlite3.connect(path)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (metadata,)).fetchone():
            return None
        found = connection.execute(f'SELECT fingerprint FROM {quote(metadata)} WHERE version=1').fetchone()
        return found[0] if found else None


def _read_definition_json(path):
    with Path(path).open('r', encoding='utf-8-sig') as stream:
        return json.load(stream)


def _definition_context(schema_dir, name):
    """Return source path, raw JSON, parsed document and grouped flag."""
    try:
        source = SchemaRegistry(schema_dir).source_path(name)
    except SchemaError as error:
        raise APIError(404, f'表不存在：{name}') from error
    raw = _read_definition_json(source)
    document = load_database_definition(source)
    grouped = document.is_grouped
    return source, raw, document, grouped


def _replace_definition_table(raw, old_name, replacement):
    """Replace one table in a grouped JSON document, preserving its siblings."""
    if isinstance(raw, dict) and isinstance(raw.get('tables'), list):
        result = copy.deepcopy(raw)
        replaced = False
        for index, item in enumerate(result['tables']):
            if isinstance(item, dict) and isinstance(item.get('head'), dict) and item['head'].get('eng_name') == old_name:
                result['tables'][index] = copy.deepcopy(replacement)
                replaced = True
                break
        if not replaced:
            raise APIError(404, f'定义文件中不存在表：{old_name}')
        return result
    return copy.deepcopy(replacement)


def _rename_shared_table(connection, old_name, new_name):
    """Rename a logical table's physical objects inside a shared SQLite file."""
    old_data, new_data = f'rtdb_data_{old_name}', f'rtdb_data_{new_name}'
    old_meta, new_meta = f'rtdb_metadata_{old_name}', f'rtdb_metadata_{new_name}'
    quote = lambda value: '"' + value.replace('"', '""') + '"'
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (new_data,)).fetchone():
        raise APIError(409, f'目标表名已存在：{new_name}')
    for suffix in ('count_insert', 'count_delete'):
        connection.execute(f'DROP TRIGGER IF EXISTS {quote(old_data + "_" + suffix)}')
    connection.execute(f'ALTER TABLE {quote(old_data)} RENAME TO {quote(new_data)}')
    connection.execute(f'ALTER TABLE {quote(old_meta)} RENAME TO {quote(new_meta)}')
    connection.execute(f'CREATE TRIGGER {quote(new_data + "_count_insert")} AFTER INSERT ON {quote(new_data)} '
                       f'BEGIN UPDATE {quote(new_meta)} SET row_count=row_count+1; END')
    connection.execute(f'CREATE TRIGGER {quote(new_data + "_count_delete")} AFTER DELETE ON {quote(new_data)} '
                       f'BEGIN UPDATE {quote(new_meta)} SET row_count=row_count-1; END')


def _sqlite_bundle(root, name):
    base = Path(root) / (name + '.sqlite3')
    return [base, Path(str(base) + '-wal'), Path(str(base) + '-shm')]


def _remove_sqlite_bundle(root, name):
    for path in _sqlite_bundle(root, name):
        path.unlink(missing_ok=True)


def _rename_sqlite_bundle(root, old_name, new_name):
    source = _sqlite_bundle(root, old_name)
    target = _sqlite_bundle(root, new_name)
    if any(path.exists() for path in target):
        raise APIError(409, f'目标表名已存在：{new_name}')
    for source_path, target_path in zip(source, target):
        if source_path.exists():
            os.replace(source_path, target_path)


_DATABASE_FILE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\.sqlite3$')
_DATABASE_JSON_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\.json$')
_DATABASE_SIDE_FILES = ('-wal', '-shm')
_MAX_DATABASE_UPLOAD_BYTES = 16 * 1024 * 1024


def _file_mtime(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec='seconds')
    except (OSError, ValueError):
        return None


def _normalise_column_renames(old, desired, value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise APIError(400, '字段改名映射必须是对象')
    old_names, new_names = set(old.column_names), set(desired.column_names)
    renames = {}
    for source, target in value.items():
        if not isinstance(source, str) or not isinstance(target, str) or not source or not target:
            raise APIError(422, '字段改名映射必须使用非空英文名')
        if source == target:
            continue
        if source not in old_names:
            raise APIError(422, f'字段改名前名称不存在：{source}')
        if target not in new_names:
            raise APIError(422, f'字段改名后名称不存在：{target}')
        renames[source] = target
    if len(set(renames.values())) != len(renames):
        raise APIError(422, '字段改名后的英文名不能重复')
    unchanged = old_names - set(renames)
    collision = set(renames.values()) & unchanged
    if collision:
        raise APIError(422, '字段改名后的英文名与未改名字段冲突：' + ', '.join(sorted(collision)))
    return renames


def _convert_schema_value(value, column):
    if value is None:
        return None
    kind = column.data_type.strip().lower()
    try:
        if kind in _INT:
            if isinstance(value, bool):
                raise ValueError('布尔值不能转换为整数')
            if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
                raise ValueError('小数不能转换为整数')
            if isinstance(value, str) and not re.fullmatch(r'[+-]?\d+', value.strip()):
                raise ValueError('必须为整数')
            return int(value)
        if kind in _FLOAT:
            if isinstance(value, bool):
                raise ValueError('布尔值不能转换为浮点数')
            result = float(value)
            if not math.isfinite(result):
                raise ValueError('必须为有限数值')
            return result
        if kind in _BOOL:
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text not in ('true', 'false', '0', '1'):
                raise ValueError('布尔值应为 true/false 或 0/1')
            return text in ('true', '1')
        if kind in _TEXT:
            return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        if kind in _OBJECT:
            result = json.loads(value) if isinstance(value, str) else value
            if kind in {'list', 'array'} and not isinstance(result, list):
                raise ValueError('必须为数组')
            if kind in {'dict', 'object'} and not isinstance(result, (dict, list)):
                raise ValueError('必须为对象或数组')
            if not isinstance(result, (dict, list)):
                raise ValueError('必须为对象或数组')
            return result
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise APIError(422, f'字段 {column.eng_name} 的数据无法转换为 {column.data_type}：{error}') from error
    raise APIError(422, f'字段 {column.eng_name} 的类型不受支持：{column.data_type}')


def _encode_schema_value(value, column, packer):
    if value is None:
        return None
    kind = column.data_type.strip().lower()
    if kind in _OBJECT:
        return packer.packb(value, use_bin_type=True)
    if kind in _FLOAT:
        return float(value)
    if kind in _BOOL:
        return int(bool(value))
    if kind in _INT and not -(2**63) <= value < 2**63:
        raise APIError(422, f'字段 {column.eng_name} 的整数超出 SQLite 范围')
    if isinstance(value, float) and not math.isfinite(value):
        raise APIError(422, f'字段 {column.eng_name} 不能保存非有限数值')
    return value


def _schema_rows(table, connection, old, desired, renames):
    source_by_target = {name: name for name in old.column_names if name not in renames}
    source_by_target.update({target: source for source, target in renames.items()})
    fields = ','.join(table._fields.values())
    result = []
    for raw in connection.execute(f'SELECT _pos,{fields} FROM {table._data_sql} ORDER BY _pos'):
        old_row = table._decode(raw[1:])
        row = {}
        for column in desired.columns:
            source = source_by_target.get(column.eng_name)
            row[column.eng_name] = _convert_schema_value(old_row.get(source), column) if source else None
        result.append((raw[0], row))
    desired.validate_rows([row for _position, row in result])
    return result


def validate_populated_schema(old, desired):
    """Keep dbdef/E imports conservative when they replace a populated schema."""
    if old.primary_keys != desired.primary_keys:
        raise APIError(409, '有数据时不能修改主键（包括新增或删除主键字段）；可以增删非主键字段')
    old_columns = {column.eng_name: column for column in old.columns}
    new_columns = {column.eng_name: column for column in desired.columns}
    for name in old_columns.keys() & new_columns.keys():
        if old_columns[name].data_type.lower() != new_columns[name].data_type.lower():
            raise APIError(409, f'有数据时不能修改已有字段类型：{name}；可以增删非主键字段')
    if [name for name in old_columns if name in new_columns] != [name for name in new_columns if name in old_columns]:
        raise APIError(409, '有数据时不能修改已有字段的相对顺序；可以增删非主键字段')


def rebuild_schema_data(connection, old, desired, rows=None, encoder=None, table=None):
    """Rebuild cN storage inside the caller's transaction, preserving row positions."""
    data_name = table._data_name if table is not None else 'data'
    data_sql = table._data_sql if table is not None else '"data"'
    metadata_sql = table._metadata_sql if table is not None else '"metadata"'
    next_name = data_name + '_schema_next'
    next_sql = '"' + next_name.replace('"', '""') + '"'
    sequence = connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (data_name,)).fetchone()
    high_water = sequence[0] if sequence else 0
    parts = ['_pos INTEGER PRIMARY KEY AUTOINCREMENT']
    for i, column in enumerate(desired.columns):
        kind = column.data_type.lower()
        affinity = 'INTEGER' if kind in _INT | _BOOL else 'REAL' if kind in _FLOAT else 'TEXT' if kind in _TEXT else 'BLOB'
        parts.append(f'c{i} {affinity}' + (' NOT NULL' if column.primary_key else ''))
    connection.execute(f'CREATE TABLE {next_sql} (' + ','.join(parts) + ')')
    old_fields = {column.eng_name: f'c{i}' for i, column in enumerate(old.columns)}
    fields = ','.join(f'c{i}' for i in range(len(desired.columns)))
    if rows is None:
        sources = ','.join(old_fields.get(column.eng_name, 'NULL') for column in desired.columns)
        connection.execute(f'INSERT INTO {next_sql} (_pos,{fields}) SELECT _pos,{sources} FROM {data_sql}')
    else:
        if encoder is None:
            raise ValueError('an encoder is required for converted schema rows')
        values = [(position, *(encoder(row.get(column.eng_name), column) for column in desired.columns))
                  for position, row in rows]
        connection.executemany(f'INSERT INTO {next_sql} (_pos,{fields}) VALUES (' +
                               ','.join('?' for _ in range(len(desired.columns) + 1)) + ')', values)
    connection.execute(f'DROP TABLE {data_sql}')
    connection.execute(f'ALTER TABLE {next_sql} RENAME TO "{data_name}"')
    # Preserve deleted rows' high-water mark, including migrations of empty tables.
    connection.execute('DELETE FROM sqlite_sequence WHERE name=?', (data_name,))
    connection.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)', (data_name, high_water))
    index_prefix = data_name if table is not None else ''
    primary_index = f'{index_prefix}_primary_key' if index_prefix else 'primary_key'
    connection.execute(f'CREATE UNIQUE INDEX "{primary_index}" ON {data_sql} (' + ','.join(f'c{i}' for i, column in enumerate(desired.columns) if column.primary_key) + ')')
    for i, column in enumerate(desired.columns):
        if (column.eng_name,) != desired.primary_keys:
            index_name = f'{index_prefix}_idx_c{i}' if index_prefix else f'idx_c{i}'
            connection.execute(f'CREATE INDEX "{index_name}" ON {data_sql} (c{i})')
    # Copy before installing count triggers: metadata.row_count stays unchanged.
    trigger_prefix = f'{index_prefix}_' if index_prefix else ''
    connection.execute(f'CREATE TRIGGER "{trigger_prefix}count_insert" AFTER INSERT ON {data_sql} BEGIN UPDATE {metadata_sql} SET row_count=row_count+1; END')
    connection.execute(f'CREATE TRIGGER "{trigger_prefix}count_delete" AFTER DELETE ON {data_sql} BEGIN UPDATE {metadata_sql} SET row_count=row_count-1; END')


def _sql_quote_name(value):
    return '"' + str(value).replace('"', '""') + '"'


def _sqlite_declared_type(value):
    """Collapse a legacy SQLite declaration to the RTDB scalar types."""

    raw = str(value or '').upper()
    if 'INT' in raw:
        return 'int'
    if any(token in raw for token in ('REAL', 'FLOA', 'DOUB')):
        return 'float'
    # Legacy BINARY/BLOB values are exposed as STR by the RTDB contract.
    return 'str'


def _unique_columns(connection, data_name='data'):
    """Return the logical key recorded by the legacy primary_key index."""

    indexes = connection.execute(f'PRAGMA index_list({_sql_quote_name(data_name)})').fetchall()
    for row in indexes:
        # SQLite returns seq, name, unique, origin, partial.
        if len(row) >= 3 and row[1] == 'primary_key' and row[2]:
            columns = connection.execute(f'PRAGMA index_info({_sql_quote_name(row[1])})').fetchall()
            return tuple(item[2] for item in sorted(columns, key=lambda item: item[0]) if item[2])
    return ()


def _find_unique_key(connection, fields, data_name='data'):
    """Find a safe fallback key when an old file has no primary_key index."""

    recorded = _unique_columns(connection, data_name)
    if recorded and all(field in fields for field in recorded):
        return recorded
    if not fields:
        raise APIError(422, '旧数据库没有可用的数据字段')
    table_sql = _sql_quote_name(data_name)
    row_count = connection.execute(f'SELECT COUNT(*) FROM {table_sql}').fetchone()[0]
    if row_count == 0:
        return (fields[0],)

    def valid(columns):
        null_clause = ' OR '.join(f'{_sql_quote_name(column)} IS NULL' for column in columns)
        grouped = ','.join(_sql_quote_name(column) for column in columns)
        if connection.execute(f'SELECT 1 FROM {table_sql} WHERE {null_clause} LIMIT 1').fetchone():
            return False
        return connection.execute(
            f'SELECT 1 FROM {table_sql} GROUP BY {grouped} HAVING COUNT(*) > 1 LIMIT 1'
        ).fetchone() is None

    for size in range(1, min(3, len(fields)) + 1):
        for columns in itertools.combinations(fields, size):
            if valid(columns):
                return columns
    raise APIError(422, '旧数据库没有可自动确定的唯一主键，请先提供正式结构定义')


def _legacy_definition(connection, name, path):
    """Build a conservative cN definition for a pre-embedded RTDB file."""

    table_sql = _sql_quote_name('data')
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='data'"
    ).fetchone():
        raise APIError(422, f'{path.name} 缺少旧 RTDB data 表，无法自动生成内置定义')
    pragma = connection.execute(f'PRAGMA table_info({table_sql})').fetchall()
    fields = [row for row in pragma if row[1] != '_pos']
    expected = [f'c{index}' for index in range(len(fields))]
    actual = [row[1] for row in fields]
    if actual != expected:
        raise APIError(422, f'{path.name} 的 data 字段不是连续的 c0...cN，不能安全自动生成定义')
    primary_keys = _find_unique_key(connection, actual)
    columns = []
    for row in fields:
        kind = _sqlite_declared_type(row[2])
        columns.append({
            'eng_name': row[1], 'chn_name': row[1], 'data_type': kind,
            'primary_key': row[1] in primary_keys,
            'disp_type': '整数' if kind == 'int' else '浮点数' if kind == 'float' else '文本',
            'disp_menu': '',
        })
    definition = TableDefinition.from_mapping(
        {'head': {'eng_name': name, 'chn_name': name}, 'columns': columns},
        path.parent, name,
    )
    count = connection.execute(f'SELECT COUNT(*) FROM {table_sql}').fetchone()[0]
    return definition, tuple(primary_keys), int(count)


def _archive_sqlite_bundle(data_dir, path, label):
    archive = Path(data_dir) / '.web-archive' / f'{path.stem}-{label}-{uuid.uuid4().hex}'
    archive.mkdir(parents=True, exist_ok=True)
    for source in _sqlite_bundle(data_dir, path.stem):
        if source.exists():
            shutil.copy2(source, archive / source.name)
    return archive


class SQLiteManager:
    max_rows = 100000

    def __init__(self, data_dir, schema_dir, *, demo=False):
        self.data_dir = Path(data_dir).resolve()
        self.schema_dir = Path(schema_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.schema_dir.mkdir(parents=True, exist_ok=True)
        self.demo = demo
        self.audit_log = OperationAuditLog(self.data_dir)
        self._gate = threading.RLock()
        self._schema_lock = self.schema_dir / '.sqlite-web.lock'
        self._journal = self.schema_dir / '.sqlite-web-pending.json'
        with self._gate, _locked(self._schema_lock):
            self._recover()

    @staticmethod
    def name(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
            raise APIError(400, '表名必须是英文标识符')
        if value.lower() in ('data', 'metadata') or value.lower().startswith(('sqlite_', 'rtdb_')):
            raise APIError(400, '该表名由 SQLite 存储内核保留')
        return value

    def definition(self, name):
        name = self.name(name)
        embedded = find_embedded_table(self.data_dir, name)
        if embedded is not None:
            return embedded[0]
        try:
            return SchemaRegistry(self.schema_dir).load(name)
        except SchemaError as error:
            raise APIError(404, f'表不存在：{name}') from error

    def definition_source(self, name):
        name = self.name(name)
        embedded = find_embedded_table(self.data_dir, name)
        if embedded is not None:
            return self.schema_dir / (embedded[1].path.stem + '.json')
        try:
            return SchemaRegistry(self.schema_dir).source_path(name)
        except SchemaError as error:
            raise APIError(404, f'表不存在：{name}') from error

    def definition_context(self, name):
        """Return the runtime definition context, preferring SQLite metadata."""
        name = self.name(name)
        embedded = find_embedded_table(self.data_dir, name)
        if embedded is not None:
            _definition, document = embedded
            return (self.schema_dir / (document.path.stem + '.json'),
                    document.to_mapping(), document, document.is_grouped)
        return _definition_context(self.schema_dir, name)

    def database_path(self, name, *, fallback=None):
        """Resolve the SQLite file that owns a logical table."""
        embedded = find_embedded_table(self.data_dir, self.name(name))
        if embedded is not None:
            return embedded[1].path
        try:
            source = self.definition_source(name)
            return self.data_dir / (source.stem + '.sqlite3')
        except APIError:
            return self.data_dir / ((fallback or name) + '.sqlite3')

    def _recover(self):
        if not self._journal.exists():
            return
        pending = json.loads(self._journal.read_text(encoding='utf-8'))
        if pending.get('grouped'):
            schema_path = Path(pending['schema_path'])
            python_path = schema_path.with_suffix('.py')
            database_path = Path(pending['database_path'])
            old_fingerprint = fingerprint(pending['old'])
            new_fingerprint = fingerprint(pending['new'])
            actual = _sqlite_table_fingerprint(database_path, pending.get('physical_name', pending['old_name']))
            if actual == new_fingerprint:
                atomic_json(schema_path, pending['new_document'])
                if pending.get('python'):
                    atomic_text(python_path, python_definition(pending['new_document']))
            elif actual == old_fingerprint:
                atomic_json(schema_path, pending['old_document'])
                if pending.get('python'):
                    atomic_text(python_path, python_definition(pending['old_document']))
            else:
                raise APIError(409, '待恢复多表数据库的结构指纹不匹配，请检查结构变更日志')
            self._journal.unlink()
            return
        old_name = self.name(pending.get('old_name', pending['name']))
        new_name = self.name(pending.get('new_name', pending['name']))
        old_definition, new_definition = pending.get('old'), pending.get('new')
        old_fingerprint = fingerprint(old_definition) if old_definition else None
        new_fingerprint = fingerprint(new_definition) if new_definition else None
        old_path = self.data_dir / (old_name + '.sqlite3')
        new_path = self.data_dir / (new_name + '.sqlite3')
        old_actual = _sqlite_fingerprint(old_path)
        new_actual = _sqlite_fingerprint(new_path) if new_name != old_name else None

        if old_name == new_name:
            actual = old_actual
            if actual is not None and actual not in {old_fingerprint, new_fingerprint}:
                raise APIError(409, '待恢复表的结构指纹不匹配，请检查结构变更日志')
            target = self.schema_dir / (old_name + '.json')
            definition = new_definition if actual == new_fingerprint else old_definition
            if definition is None:
                target.unlink(missing_ok=True)
                if pending.get('python'):
                    target.with_suffix('.py').unlink(missing_ok=True)
                if pending.get('created') and actual is None:
                    _remove_sqlite_bundle(self.data_dir, old_name)
            else:
                atomic_json(target, definition)
                if pending.get('python'):
                    atomic_text(target.with_suffix('.py'), python_definition(definition))
            self._journal.unlink()
            return

        if new_actual is not None and new_actual not in {old_fingerprint, new_fingerprint}:
            raise APIError(409, '待恢复表的结构指纹不匹配，请检查结构变更日志')
        if old_actual is not None and old_actual not in {old_fingerprint, new_fingerprint}:
            raise APIError(409, '待恢复表的结构指纹不匹配，请检查结构变更日志')

        old_schema = self.schema_dir / (old_name + '.json')
        new_schema = self.schema_dir / (new_name + '.json')
        if old_actual == new_fingerprint or new_actual == new_fingerprint:
            if new_actual is None:
                _rename_sqlite_bundle(self.data_dir, old_name, new_name)
            atomic_json(new_schema, new_definition)
            if pending.get('python'):
                atomic_text(new_schema.with_suffix('.py'), python_definition(new_definition))
            old_schema.unlink(missing_ok=True)
            if pending.get('python'):
                old_schema.with_suffix('.py').unlink(missing_ok=True)
        elif old_actual == old_fingerprint or old_actual is None:
            atomic_json(old_schema, old_definition) if old_definition else old_schema.unlink(missing_ok=True)
            if pending.get('python'):
                if old_definition:
                    atomic_text(old_schema.with_suffix('.py'), python_definition(old_definition))
                else:
                    old_schema.with_suffix('.py').unlink(missing_ok=True)
            new_schema.unlink(missing_ok=True)
            if pending.get('python'):
                new_schema.with_suffix('.py').unlink(missing_ok=True)
            if new_actual is not None or new_path.exists():
                _remove_sqlite_bundle(self.data_dir, new_name)
        else:
            raise APIError(409, '待恢复表的结构状态不明确，请检查结构变更日志')
        self._journal.unlink()

    @contextmanager
    def session(self, name=None):
        # Serializes schema changes with request connections in this service.
        # SQLite still coordinates independently opened external processes.
        with self._gate, _locked(self._schema_lock):
            self._recover()
            if name is not None:
                self.definition(name)
            with SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir) as db:
                yield db if name is None else db[name]

    def info(self):
        return dict(engine='sqlite', sqlite_version=sqlite3.sqlite_version,
                    data_dir=str(self.data_dir), schema_dir=str(self.schema_dir), demo=self.demo,
                    audit_log=str(self.audit_log.log_dir), audit_log_legacy=str(self.audit_log.legacy_path),
                    audit_log_config=str(self.audit_log.config_path),
                    log_retention_days=self.audit_log.config().get('retention_days', 0),
                    durability='WAL / FULL', page_limit=500,
                    export_limit=self.max_rows)

    def _database_file_snapshot(self, path):
        """Inspect one server-side SQLite file without registering it first."""
        name = path.name[:-len('.sqlite3')]
        schema_path = self.schema_dir / (name + '.json')
        python_path = self.schema_dir / (name + '.py')
        item = dict(name=name, filename=path.name, path=str(path), size_bytes=path.stat().st_size,
                    modified_at=_file_mtime(path), definition_exists=schema_path.is_file(),
                    embedded_definition_exists=False,
                    definition_source=None,
                    python_exists=python_path.is_file(), rtdb=False, selectable=False,
                    row_count=None, stored_fingerprint=None, definition_match=False, tables=[],
                    status='未检查', error=None)
        try:
            # The embedded registry is authoritative even when an old/stale
            # JSON sidecar is still present. JSON remains a compatibility
            # source only for files created before embedded schema support.
            document = load_embedded_database(path)
            if document is not None:
                item['embedded_definition_exists'] = True
                item['definition_source'] = 'SQLite 内置结构'
            elif schema_path.is_file():
                document = load_database_definition(schema_path)
                item['definition_source'] = 'JSON 兼容定义'
        except (OSError, SchemaError, json.JSONDecodeError) as error:
            item.update(status='SQLite 内置结构无效' if item['embedded_definition_exists']
                        else '定义文件无效', error=str(error))
            return item
        if document is None:
            item.update(status='缺少内置结构定义', error=f'缺少 {path.name} 内置结构定义（旧文件可通过同名 JSON 首次加载迁移）')
            return item
        shared = document.is_grouped
        definitions = {table.eng_name: table for table in document.tables}
        if not shared and name not in definitions:
            item.update(status='定义表名不匹配', error=f'JSON 表名为 {next(iter(definitions), "未知")}')
            return item
        try:
            with closing(sqlite3.connect(str(path))) as connection:
                integrity = connection.execute('PRAGMA quick_check').fetchone()[0]
                if integrity != 'ok':
                    item.update(status='完整性检查失败', error=f'quick_check={integrity}')
                    return item
                total = 0
                matches = True
                for table_name, definition in definitions.items():
                    data_name = 'data' if not shared else f'rtdb_data_{table_name}'
                    metadata_name = 'metadata' if not shared else f'rtdb_metadata_{table_name}'
                    quote = lambda value: '"' + value.replace('"', '""') + '"'
                    metadata = connection.execute(f'SELECT row_count, fingerprint FROM {quote(metadata_name)} WHERE version=1').fetchone()
                    if metadata is None:
                        raise sqlite3.DatabaseError(f'缺少 {metadata_name} 元数据')
                    row_count, stored_fingerprint = metadata
                    if not isinstance(row_count, int) or row_count < 0 or not isinstance(stored_fingerprint, str):
                        raise sqlite3.DatabaseError(f'{metadata_name} 元数据无效')
                    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (data_name,)).fetchone():
                        raise sqlite3.DatabaseError(f'缺少 {data_name} 数据表')
                    actual_count = connection.execute(f'SELECT COUNT(*) FROM {quote(data_name)}').fetchone()[0]
                    table_match = actual_count == row_count and stored_fingerprint == fingerprint(definition.to_mapping())
                    matches = matches and table_match
                    total += row_count
                    item['tables'].append(dict(name=table_name, row_count=row_count,
                                               stored_fingerprint=stored_fingerprint,
                                               definition_match=table_match))
                item.update(rtdb=True, row_count=total, stored_fingerprint=None,
                            definition_match=matches)
        except (OSError, sqlite3.Error) as error:
            item.update(status='无法读取', error=str(error))
            return item
        if not item['definition_match']:
            source = item['definition_source'] or '结构定义'
            item.update(status='结构指纹不匹配', error=f'SQLite metadata 与 {source} 不一致')
            return item
        item.update(status='可用' if item['embedded_definition_exists'] else '可用（JSON 兼容，首次打开后内置化）', selectable=True)
        return item

    def database_files(self):
        files = []
        for path in sorted(self.data_dir.glob('*.sqlite3'), key=lambda candidate: candidate.name.lower()):
            if not _DATABASE_FILE_RE.fullmatch(path.name):
                continue
            try:
                files.append(self._database_file_snapshot(path))
            except OSError as error:
                files.append({'name': path.stem, 'filename': path.name, 'path': str(path),
                              'size_bytes': 0, 'modified_at': None, 'definition_exists': False,
                              'python_exists': False, 'rtdb': False, 'selectable': False,
                              'row_count': None, 'stored_fingerprint': None, 'definition_match': False,
                              'status': '无法读取', 'error': str(error)})
        return {'files': files, 'data_dir': str(self.data_dir), 'schema_dir': str(self.schema_dir)}

    def select_database_file(self, payload):
        filename = payload.get('filename') if isinstance(payload, dict) else None
        if not isinstance(filename, str) or not _DATABASE_FILE_RE.fullmatch(filename):
            raise APIError(400, '请选择有效的 .sqlite3 数据库文件')
        path = self.data_dir / filename
        if not path.is_file():
            raise APIError(404, f'服务端文件不存在：{filename}')
        item = self._database_file_snapshot(path)
        if not item['selectable']:
            raise APIError(409, f'文件不可加载：{item.get("error") or item["status"]}')
        return {'selected': True, 'name': item['name'], 'filename': filename, 'file': item}

    @staticmethod
    def _database_filename(payload):
        filename = payload.get('filename') if isinstance(payload, dict) else None
        if not isinstance(filename, str) or not _DATABASE_FILE_RE.fullmatch(filename):
            raise APIError(400, '请选择有效的 .sqlite3 数据库文件')
        return filename

    def rebuild_embedded_definition(self, payload):
        """Register a legacy cN RTDB file using its physical SQLite layout.

        This action deliberately keeps the legacy column names (c0, c1, ...).
        It makes the file usable and auditable without pretending that missing
        business names can be recovered from SQLite affinity alone.
        """

        filename = self._database_filename(payload)
        path = self.data_dir / filename
        if not path.is_file():
            raise APIError(404, f'服务端文件不存在：{filename}')
        with self._gate, _locked(self._schema_lock):
            self._recover()
            current = self._database_file_snapshot(path)
            if current.get('embedded_definition_exists'):
                return {'generated': False, 'filename': filename, 'name': path.stem,
                        'message': '文件已经存在 SQLite 内置结构定义', 'file': current}
            archive = _archive_sqlite_bundle(self.data_dir, path, 'before-embedded-definition')
            try:
                with closing(sqlite3.connect(str(path), timeout=10, isolation_level=None)) as connection:
                    connection.execute('PRAGMA journal_mode=WAL')
                    connection.execute('PRAGMA synchronous=FULL')
                    connection.execute('BEGIN IMMEDIATE')
                    try:
                        definition, primary_keys, row_count = _legacy_definition(connection, path.stem, path)
                        ensure_embedded_schema(connection, path.stem, path.stem)
                        write_embedded_definition(connection, definition, path.stem, path.stem)
                        connection.execute(
                            'CREATE TABLE IF NOT EXISTS "metadata" ('
                            'version INTEGER PRIMARY KEY CHECK(version=1), '
                            'fingerprint TEXT NOT NULL, row_count INTEGER NOT NULL)'
                        )
                        connection.execute(
                            'INSERT INTO "metadata"(version, fingerprint, row_count) VALUES (1, ?, ?) '
                            'ON CONFLICT(version) DO UPDATE SET fingerprint=excluded.fingerprint, '
                            'row_count=excluded.row_count',
                            (fingerprint(definition.to_mapping()), row_count),
                        )
                        existing_index = next(
                            (row for row in connection.execute('PRAGMA index_list("data")').fetchall()
                             if len(row) >= 2 and row[1] == 'primary_key'),
                            None,
                        )
                        existing_columns = _unique_columns(connection, 'data')
                        if existing_index is None or not existing_index[2] or existing_columns != primary_keys:
                            connection.execute('DROP INDEX IF EXISTS "primary_key"')
                            connection.execute(
                                'CREATE UNIQUE INDEX "primary_key" ON "data" (' +
                                ','.join(_sql_quote_name(column) for column in primary_keys) + ')'
                            )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                definition_mapping = definition.to_mapping()
                atomic_json(self.schema_dir / (path.stem + '.json'), definition_mapping)
                atomic_text(self.schema_dir / (path.stem + '.py'), python_definition(definition_mapping))
            except BaseException:
                raise
        item = self._database_file_snapshot(path)
        return {
            'generated': True, 'filename': filename, 'name': path.stem,
            'primary_keys': list(primary_keys), 'row_count': row_count,
            'definition': definition_mapping, 'backup': str(archive), 'file': item,
            'warning': '缺失的业务字段名称无法从旧 SQLite 文件恢复，当前使用物理字段名 c0、c1……；请用正式 dbdef/E 定义更新。',
        }

    def delete_orphan_database_file(self, payload):
        """Archive an unregistered database file after an explicit confirmation."""

        filename = self._database_filename(payload)
        if payload.get('confirm') != filename:
            raise APIError(400, f'请输入文件名确认删除：{filename}')
        path = self.data_dir / filename
        if not path.is_file():
            raise APIError(404, f'服务端文件不存在：{filename}')
        with self._gate, _locked(self._schema_lock):
            self._recover()
            item = self._database_file_snapshot(path)
            if item.get('selectable') or item.get('embedded_definition_exists'):
                raise APIError(409, '该文件已有可用定义，不能通过未定义文件入口删除')
            if item.get('status') != '缺少内置结构定义':
                raise APIError(409, f'当前文件不是“缺少内置结构定义”状态：{item.get("status")}')
            archive = self.data_dir / '.web-archive' / f'{path.stem}-orphan-{uuid.uuid4().hex}'
            archive.mkdir(parents=True, exist_ok=True)
            moved = []
            candidates = _sqlite_bundle(self.data_dir, path.stem) + [
                self.schema_dir / (path.stem + '.json'), self.schema_dir / (path.stem + '.py')]
            try:
                for source in candidates:
                    if not source.exists():
                        continue
                    target = archive / source.name
                    os.replace(source, target)
                    moved.append((source, target))
            except BaseException:
                for source, target in reversed(moved):
                    if target.exists():
                        os.replace(target, source)
                raise
        return {'deleted': True, 'filename': filename, 'name': path.stem, 'archive': str(archive)}

    def rebuild_from_embedded_definition(self, payload):
        """Rebuild legacy physical data tables from SQLite embedded metadata."""

        filename = self._database_filename(payload)
        path = self.data_dir / filename
        if not path.is_file():
            raise APIError(404, f'服务端文件不存在：{filename}')
        with self._gate, _locked(self._schema_lock):
            self._recover()
            document = load_embedded_database(path)
            if document is None:
                raise APIError(409, '当前文件没有 SQLite 内置定义，请先执行“重新生成内置定义表”或加载正式定义')
            archive = _archive_sqlite_bundle(self.data_dir, path, 'before-embedded-rebuild')
            with closing(sqlite3.connect(str(path), timeout=10, isolation_level=None)) as connection:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('BEGIN IMMEDIATE')
                try:
                    for definition in document.tables:
                        shared = document.is_grouped
                        data_name = 'data' if not shared else f'rtdb_data_{definition.eng_name}'
                        metadata_name = 'metadata' if not shared else f'rtdb_metadata_{definition.eng_name}'
                        data_sql = _sql_quote_name(data_name)
                        metadata_sql = _sql_quote_name(metadata_name)
                        if not connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (data_name,)
                        ).fetchone():
                            raise APIError(422, f'{definition.eng_name} 缺少物理数据表：{data_name}')
                        physical = connection.execute(f'PRAGMA table_info({data_sql})').fetchall()
                        fields = [row for row in physical if row[1] != '_pos']
                        expected = [f'c{index}' for index in range(len(fields))]
                        if [row[1] for row in fields] != expected or len(fields) != len(definition.columns):
                            raise APIError(422, f'{definition.eng_name} 的物理字段数量或顺序与内置定义不一致')
                        old_mapping = {
                            'head': definition.to_mapping()['head'],
                            'columns': [dict(column, data_type=_sqlite_declared_type(row[2]))
                                        for column, row in zip(definition.to_mapping()['columns'], fields)],
                        }
                        old_definition = TableDefinition.from_mapping(old_mapping, path.parent, definition.eng_name)
                        stub = type('PhysicalTable', (), {
                            '_data_name': data_name, '_data_sql': data_sql,
                            '_metadata_name': metadata_name, '_metadata_sql': metadata_sql,
                        })()
                        rebuild_schema_data(connection, old_definition, definition, table=stub if shared else None)
                        count = connection.execute(f'SELECT COUNT(*) FROM {data_sql}').fetchone()[0]
                        connection.execute(
                            f'UPDATE {metadata_sql} SET fingerprint=?, row_count=? WHERE version=1',
                            (fingerprint(definition.to_mapping()), count),
                        )
                        write_embedded_definition(connection, definition, path.stem, document.chn_name)
                    integrity = connection.execute('PRAGMA quick_check').fetchone()[0]
                    if integrity != 'ok':
                        raise APIError(422, f'重建后 SQLite 完整性检查失败：{integrity}')
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            output = document.to_mapping()
            atomic_json(self.schema_dir / (path.stem + '.json'), output)
            atomic_text(self.schema_dir / (path.stem + '.py'), python_definition(output))
        item = self._database_file_snapshot(path)
        return {'rebuilt': True, 'filename': filename, 'name': path.stem,
                'backup': str(archive), 'file': item, 'tables': list(document.table_names)}

    def create_database_file(self, payload):
        if not isinstance(payload, dict):
            raise APIError(400, '新建数据库文件参数无效')
        name = self.name(payload.get('name', ''))
        label = normalize_definition_labels({'head': {'eng_name': name, 'chn_name': payload.get('chn_name')}})['head']['chn_name']
        if not isinstance(label, str) or not label.strip():
            raise APIError(400, '中文表名不能为空')
        grouped_values = payload.get('tables')
        if grouped_values is not None:
            if not isinstance(grouped_values, list) or not grouped_values:
                raise APIError(400, 'tables 必须是非空表结构数组')
            definitions = []
            names = set()
            for value in grouped_values:
                if not isinstance(value, dict):
                    raise APIError(400, 'tables 中每项必须是表结构对象')
                value = copy.deepcopy(value)
                if not isinstance(value.get('head'), dict):
                    raise APIError(400, 'tables 中每项必须包含 head 对象')
                table_name = self.name(value['head'].get('eng_name', ''))
                if table_name in names:
                    raise APIError(400, f'重复表名：{table_name}')
                names.add(table_name)
                value['head']['chn_name'] = normalize_definition_labels(value)['head']['chn_name'] or table_name
                try:
                    definitions.append(TableDefinition.from_mapping(value, self.schema_dir, table_name))
                except SchemaError as error:
                    raise APIError(422, f'{table_name} 定义无效：{error}') from error
            target_json = self.schema_dir / (name + '.json')
            target_db = self.data_dir / (name + '.sqlite3')
            target_python = self.schema_dir / (name + '.py')
            if any(path.exists() for path in (target_json, target_db, target_python)):
                raise APIError(409, f'数据库文件已存在：{name}.sqlite3')
            document = {'database': {'eng_name': name, 'chn_name': label.strip()},
                        'tables': [definition.to_mapping() for definition in definitions]}
            atomic_json(target_json, document)
            try:
                with SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir) as db:
                    for definition in definitions:
                        db[definition.eng_name]
                atomic_text(target_python, python_definition(document))
            except BaseException:
                target_json.unlink(missing_ok=True)
                target_db.unlink(missing_ok=True)
                target_python.unlink(missing_ok=True)
                raise
            return {'created': True, 'filename': target_db.name, 'name': name,
                    'tables': [definition.eng_name for definition in definitions]}
        definition = payload.get('definition')
        if definition is None:
            definition = {
                'head': {'eng_name': name, 'chn_name': label.strip()},
                'columns': [
                    {'eng_name': 'id', 'chn_name': '编号', 'data_type': 'int', 'primary_key': True,
                     'disp_type': '数字', 'disp_menu': ''},
                    {'eng_name': 'name', 'chn_name': '名称', 'data_type': 'str', 'primary_key': False,
                     'disp_type': '文本', 'disp_menu': ''},
                ],
            }
        elif not isinstance(definition, dict):
            raise APIError(400, '表结构定义必须是对象')
        else:
            definition = json.loads(json.dumps(definition, ensure_ascii=False))
            definition.setdefault('head', {})['eng_name'] = name
            definition['head'].setdefault('chn_name', label.strip())
        result = self.save_schema(name, {'definition': definition}, creating=True)
        return {'created': True, 'filename': name + '.sqlite3', **result}

    @staticmethod
    def _decode_database_upload(item):
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('content'), str):
            raise APIError(400, '上传文件项必须包含 name 和 Base64 content')
        try:
            content = base64.b64decode(item['content'], validate=True)
        except (ValueError, TypeError) as error:
            raise APIError(400, f'文件 {item.get("name", "")} 不是有效的 Base64') from error
        if not content or len(content) > _MAX_DATABASE_UPLOAD_BYTES:
            raise APIError(413, f'文件 {item.get("name", "")} 必须为非空且不超过 16 MiB')
        return item['name'], content

    def upload_database_file(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('files'), list) or not payload['files']:
            raise APIError(400, '请选择至少一个数据库文件')
        if len(payload['files']) > 4:
            raise APIError(400, '最多同时上传数据库、JSON 定义及两个 SQLite 辅助文件')
        decoded = [self._decode_database_upload(item) for item in payload['files']]
        names = [name for name, _content in decoded]
        if len(names) != len(set(names)):
            raise APIError(400, '上传文件名不能重复')
        database_items = [(name, content) for name, content in decoded if _DATABASE_FILE_RE.fullmatch(name)]
        if len(database_items) != 1:
            raise APIError(400, '必须且只能上传一个 .sqlite3 数据库文件')
        database_name, database_bytes = database_items[0]
        name = database_name[:-len('.sqlite3')]
        expected = {name + '.json', database_name + '-wal', database_name + '-shm'}
        unknown = set(names) - {database_name} - expected
        if unknown:
            raise APIError(400, '只支持同名 .json、.sqlite3-wal 和 .sqlite3-shm 文件')
        json_items = [(file_name, content) for file_name, content in decoded if _DATABASE_JSON_RE.fullmatch(file_name)]
        if len(json_items) > 1 or (json_items and json_items[0][0] != name + '.json'):
            raise APIError(400, 'JSON 定义文件必须与数据库文件同名')
        sidecars = {file_name: content for file_name, content in decoded if file_name in expected and file_name != name + '.json'}
        stage = Path(tempfile.mkdtemp(prefix='.rtdb-upload-', dir=self.data_dir))
        try:
            stage_db = stage / database_name
            stage_db.write_bytes(database_bytes)
            for suffix in _DATABASE_SIDE_FILES:
                sidecar_name = database_name + suffix
                if sidecar_name in sidecars:
                    (stage / sidecar_name).write_bytes(sidecars[sidecar_name])
            schema_path = self.schema_dir / (name + '.json')
            json_content = json_items[0][1] if json_items else None
            embedded_source = False
            if json_content is not None:
                try:
                    raw_document = json.loads(json_content.decode('utf-8-sig'))
                    stage_definition = stage / (name + '.json')
                    stage_definition.write_text(json.dumps(raw_document, ensure_ascii=False), encoding='utf-8')
                    database_document = load_database_definition(stage_definition)
                except (UnicodeDecodeError, json.JSONDecodeError, OSError, SchemaError) as error:
                    raise APIError(422, f'{name}.json 定义无效：{error}') from error
            else:
                # A self-describing SQLite upload remains valid even if the
                # server still has an old or damaged JSON sidecar. Fall back
                # to that sidecar only for legacy files without embedded
                # metadata.
                embedded_document = load_embedded_database(stage_db)
                if embedded_document is not None:
                    database_document = embedded_document
                    raw_document = embedded_document.to_mapping()
                    embedded_source = True
                elif schema_path.is_file():
                    try:
                        raw_document = _read_definition_json(schema_path)
                        database_document = load_database_definition(schema_path)
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SchemaError) as error:
                        raise APIError(422, f'{name}.json 定义无效：{error}') from error
                else:
                    raise APIError(422, f'服务端和上传文件都缺少 {name} 的结构定义')
            definitions = list(database_document.tables)
            if not definitions:
                raise APIError(422, 'JSON 定义至少需要一张表')
            grouped = database_document.is_grouped
            if not grouped and (len(definitions) != 1 or definitions[0].eng_name != name):
                raise APIError(422, f'{name}.json 的英文表名必须为 {name}')
            if grouped and database_document.eng_name != name:
                # The SQLite file name identifies the grouped database; table
                # names remain the logical names inside the document.
                raw_document = copy.deepcopy(raw_document)
                raw_document.setdefault('database', {})['eng_name'] = name

            try:
                with closing(sqlite3.connect(str(stage_db))) as connection:
                    integrity = connection.execute('PRAGMA quick_check').fetchone()[0]
                    total_count = 0
                    table_results = []
                    for definition in definitions:
                        data_name = 'data' if not grouped else f'rtdb_data_{definition.eng_name}'
                        metadata_name = 'metadata' if not grouped else f'rtdb_metadata_{definition.eng_name}'
                        quote = lambda value: '"' + value.replace('"', '""') + '"'
                        metadata = connection.execute(
                            f'SELECT row_count, fingerprint FROM {quote(metadata_name)} WHERE version=1'
                        ).fetchone()
                        if metadata is None:
                            raise APIError(422, f'上传文件缺少 {metadata_name} 元数据')
                        row_count, stored_fingerprint = metadata
                        if not connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (data_name,)
                        ).fetchone():
                            raise APIError(422, f'上传文件缺少 {data_name} 数据表')
                        actual_count = connection.execute(f'SELECT COUNT(*) FROM {quote(data_name)}').fetchone()[0]
                        if actual_count != row_count or stored_fingerprint != fingerprint(definition.to_mapping()):
                            raise APIError(409, f'{definition.eng_name} 的 SQLite metadata 与 JSON 定义不一致')
                        if not isinstance(row_count, int) or row_count < 0 or row_count > self.max_rows:
                            raise APIError(413, f'{definition.eng_name} 记录数超过 {self.max_rows} 条')
                        total_count += row_count
                        table_results.append({'name': definition.eng_name, 'row_count': row_count})
            except APIError:
                raise
            except sqlite3.Error as error:
                raise APIError(422, f'上传文件不是有效的 SQLite 数据库：{error}') from error
            if integrity != 'ok':
                raise APIError(422, f'上传文件完整性检查失败：quick_check={integrity}')
            if grouped:
                output_document = copy.deepcopy(raw_document)
                output_document['tables'] = [definition.to_mapping() for definition in definitions]
                output_document.setdefault('database', {})['eng_name'] = name
                output_document['database'].setdefault('chn_name', name)
            else:
                output_document = definitions[0].to_mapping()

            target_db = self.data_dir / database_name
            target_json = self.schema_dir / (name + '.json')
            target_python = self.schema_dir / (name + '.py')
            replaced = target_db.exists() or target_json.exists()
            if replaced and payload.get('confirm') != name:
                raise APIError(409, f'目标文件已存在，请输入 {name} 确认覆盖')
            with self._gate, _locked(self._schema_lock):
                self._recover()
                backup = Path(tempfile.mkdtemp(prefix='.rtdb-upload-backup-', dir=self.data_dir))
                paths = [target_db, *(self.data_dir / (database_name + suffix) for suffix in _DATABASE_SIDE_FILES)]
                if json_content is not None:
                    paths.extend([target_json, target_python])
                moved = []
                try:
                    for path in paths:
                        if path.exists():
                            backup_path = backup / path.name
                            os.replace(path, backup_path)
                            moved.append((path, backup_path))
                    os.replace(stage_db, target_db)
                    for suffix in _DATABASE_SIDE_FILES:
                        source = stage / (database_name + suffix)
                        if source.exists():
                            os.replace(source, self.data_dir / source.name)
                    if json_content is not None:
                        atomic_json(target_json, output_document)
                        atomic_text(target_python, python_definition(output_document))
                except BaseException:
                    for path in paths:
                        if path.exists() and path not in {item[0] for item in moved}:
                            path.unlink(missing_ok=True)
                    for path, backup_path in reversed(moved):
                        if backup_path.exists():
                            os.replace(backup_path, path)
                    raise
                finally:
                    shutil.rmtree(backup, ignore_errors=True)
            return {'ok': True, 'updated': True, 'name': name, 'filename': database_name,
                    'row_count': total_count, 'tables': table_results, 'bytes': len(database_bytes), 'replaced': replaced,
                    'definition_updated': json_content is not None, 'python_updated': json_content is not None,
                    'embedded_definition_used': embedded_source}
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def record_operation(self, *, category, operation, status, http_status, method, path,
                         table=None, duration_ms=0, result=None, error=None):
        """Persist one HTTP operation without allowing logging to mask its result."""
        failure = error
        actual_status = status
        if error is not None and not isinstance(error, dict):
            failure = error_summary(error, http_status)
        if isinstance(result, dict) and result.get('ok') is False:
            actual_status = 'failure'
            if failure is None:
                failed = next((item for item in result.get('results', [])
                               if isinstance(item, dict) and item.get('ok') is False), None)
                failure = {'type': 'OperationResultError', 'message': '操作返回失败',
                           'http_status': http_status, 'details': result_summary(failed or result)}
        self.audit_log.append(category=category, operation=operation, status=actual_status,
                              http_status=http_status, method=method, path=path, table=table,
                              duration_ms=duration_ms, result=result, error=failure)

    def tables(self):
        result = []
        known_database_files = set()
        with self.session() as db:
            names = set(db._embedded_tables())
            try:
                names.update(db.schema_registry.names())
            except SchemaError:
                # A malformed optional JSON file must not hide valid embedded
                # definitions or database files from the table index.
                pass
            for name in sorted(names):
                definition = self.definition(name)
                database_path = self.database_path(name)
                item = dict(name=name, definition=definition.to_mapping(), schema_version=fingerprint(definition.to_mapping()),
                            database_file=database_path.name, database_name=database_path.stem,
                            orphan=False, selectable=True)
                known_database_files.add(database_path.name)
                try:
                    table = db[name]
                    item.update(row_count=len(table), bytes=table.path.stat().st_size,
                                database_file=table.path.name, database_name=table.path.stem, error=None)
                    known_database_files.add(table.path.name)
                except (SchemaError, sqlite3.Error) as exc:
                    item.update(row_count=None, bytes=0, error=str(exc))
                result.append(item)
        for path in sorted(self.data_dir.glob('*.sqlite3'), key=lambda candidate: candidate.name.lower()):
            if not _DATABASE_FILE_RE.fullmatch(path.name) or path.name in known_database_files:
                continue
            name = path.stem
            definition = {'head': {'chn_name': '缺少内置结构定义', 'eng_name': name}, 'columns': []}
            result.append(dict(name=name, definition=definition, schema_version=None,
                               row_count=None, bytes=path.stat().st_size, database_file=path.name,
                               database_name=name, error=f'缺少 {name}.sqlite3 内置结构定义',
                               orphan=True, selectable=False))
        result.sort(key=lambda item: item['name'].lower())
        return {'tables': result}

    @staticmethod
    def _coerce(table, values, *, complete=False):
        if not isinstance(values, dict):
            raise APIError(400, '记录必须是对象')
        converted = dict(values)
        for name, value in converted.items():
            kind = table._types.get(name)
            if kind in _INT and isinstance(value, str):
                if not re.fullmatch(r'-?\d+', value):
                    raise APIError(422, f'{name} 必须为整数')
                converted[name] = int(value)
        return table._normalise_row(converted, complete=complete)

    @staticmethod
    def _key(table, key):
        if not isinstance(key, list) or len(key) != len(table.primary_keys):
            raise APIError(400, '主键参数数量与表定义不符')
        return SQLiteManager._coerce(table, dict(zip(table.primary_keys, key)))

    @staticmethod
    def _view(table, connection):
        name = '"' + table.name + '"'
        columns = ','.join(f'{physical} AS "{logical}"' for logical, physical in table._fields.items())
        connection.execute(f'CREATE TEMP VIEW IF NOT EXISTS {name} AS SELECT {columns} FROM main.{table._data_sql}')
        return name

    @contextmanager
    def _reader(self, table, connection):
        # Only expose the logical table. No DDL, PRAGMA, ATTACH or metadata.
        deadline = time.perf_counter() + 5
        def authorizer(action, first, second, database, source):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and (
                    database == 'temp' and first == table.name or
                    database == 'main' and first == table._data_name):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() not in ('load_extension', 'writefile', 'readfile'):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        connection.set_authorizer(authorizer)
        connection.set_progress_handler(lambda: int(time.perf_counter() > deadline), 2000)
        try:
            yield
        finally:
            connection.set_authorizer(None)
            connection.set_progress_handler(None, 0)

    def _selection(self, table, connection, where='', order='', limit=100, offset=0):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.max_rows:
            raise APIError(400, '分页数量超出允许范围')
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise APIError(400, 'offset 必须为非负整数')
        view = self._view(table, connection)
        where = where.strip() if isinstance(where, str) else ''
        order = order.strip() if isinstance(order, str) else ''
        clause = f' WHERE ({where})' if where else ''
        # Wrapping prevents trailing comments from removing the outer window.
        if not order:
            order = ','.join('"' + key + '"' for key in table.primary_keys)
        source = f'SELECT * FROM {view}{clause} ORDER BY {order}'
        with self._reader(table, connection):
            total = connection.execute(f'SELECT COUNT(*) FROM ({source}\n)').fetchone()[0]
            records = connection.execute(f'SELECT * FROM ({source}\n) LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return [table._decode(row) for row in records], total

    def rows(self, name, *, where='', order='', limit=100, offset=0):
        if limit > 500:
            raise APIError(400, '单页最多 500 行')
        with self.session(name) as table:
            with table._read_transaction() as c:
                rows, total = self._selection(table, c, where, order, limit, offset)
            return dict(rows=rows, versions=[fingerprint(r) for r in rows], total=total,
                        limit=limit, offset=offset, primary_keys=list(table.primary_keys))

    def insert(self, name, payload):
        rows = payload.get('rows')
        if not isinstance(rows, list) or len(rows) > self.max_rows:
            raise APIError(400, 'rows 必须是记录数组，且不超过导入上限')
        with self.session(name) as table:
            prepared = [self._coerce(table, row, complete=True) for row in rows]
            return {'inserted': table.insert_many(prepared)}

    def edit(self, name, payload):
        with self.session(name) as table:
            where = self._key(table, payload.get('key'))
            values = self._coerce(table, payload.get('values'))
            if any(k in values and values[k] != v for k, v in where.items()):
                raise APIError(422, '编辑时不可更改主键；请使用复制记录创建新主键')
            def update(c):
                rows = table._select(c, where)
                if not rows:
                    raise APIError(404, '记录已被删除，请刷新')
                if payload.get('version') != fingerprint(rows[0]):
                    raise APIError(409, '记录已被其他操作修改，请刷新后重新编辑')
                return table._change(c, 'update', where, values, strict=True)
            return {'updated': table._write(update)}

    def delete_rows(self, name, payload):
        items = payload.get('records')
        if not isinstance(items, list) or not 1 <= len(items) <= 500:
            raise APIError(400, '请选择 1–500 条记录')
        with self.session(name) as table:
            def remove(c):
                wheres, seen = [], set()
                for item in items:
                    if not isinstance(item, dict):
                        raise APIError(400, 'records 中每项必须是对象')
                    where = self._key(table, item.get('key'))
                    key = tuple(where.values())
                    if key in seen:
                        raise APIError(400, '重复主键')
                    seen.add(key)
                    rows = table._select(c, where)
                    if not rows or item.get('version') != fingerprint(rows[0]):
                        raise APIError(409, '所选记录已变化，删除已撤销，请刷新')
                    wheres.append(where)
                return sum(table._change(c, 'delete', where, strict=True) for where in wheres)
            return {'deleted': table._write(remove)}

    def clear(self, name, payload):
        if payload.get('confirm') != name:
            raise APIError(400, '请输入完整表名确认清空')
        with self.session(name) as table:
            def clear(c):
                count = c.execute(f'SELECT row_count FROM {table._metadata_sql}').fetchone()[0]
                if payload.get('expected_count') != count:
                    raise APIError(409, '记录数量已变化，请刷新后重试')
                return c.execute(f'DELETE FROM {table._data_sql}').rowcount
            return {'deleted': table._write(clear)}

    def sql(self, name, payload):
        self.name(name)
        statement = payload.get('sql', '')
        if not isinstance(statement, str):
            raise APIError(400, 'sql 必须是字符串')
        statement = statement.strip()
        if not statement or len(statement) > 20000:
            raise APIError(400, '请输入 SQL（最多 20,000 字符）')
        kind = statement.split(None, 1)[0].upper()
        started = time.perf_counter()
        with self.session(name) as table:
            if kind in ('SELECT', 'WITH'):
                c = table._connection()
                self._view(table, c)
                with self._reader(table, c):
                    cursor = c.execute(statement)
                    fields = [d[0] for d in cursor.description]
                    values = cursor.fetchmany(501)
                # Decode object fields when directly selected; aggregates stay scalar.
                rows = []
                for values_row in values[:500]:
                    row = {}
                    for column, value in zip(fields, values_row):
                        if column in table._types and table._types[column] in _OBJECT and isinstance(value, bytes):
                            value = table._msgpack.unpackb(value, raw=False, strict_map_key=False)
                        elif isinstance(value, bytes):
                            value = value.hex()
                        row[column] = value
                    rows.append(row)
                result = dict(rows=rows, columns=fields, rowcount=len(rows), truncated=len(values) > 500, readonly=True)
            elif kind in ('INSERT', 'UPDATE', 'DELETE'):
                if payload.get('confirm') != name:
                    raise APIError(400, '写入 SQL 需要确认表名')
                result = self._sql_write(table, statement)
            else:
                raise APIError(400, '支持 SELECT / WITH 查询，以及单条 INSERT / UPDATE / DELETE；结构请在结构编辑器修改')
        return {**result, 'elapsed_ms': round((time.perf_counter() - started) * 1000, 2)}

    def _sql_write(self, table, statement):
        """Execute one native statement through a validating writable view.

        Only the generated triggers may touch physical data. This retains
        SQLite expressions and LIKE/IN predicates without the fallback parser's
        permissive handling of trailing DELETE text.
        """
        # REPLACE can delete rows without firing count_delete when SQLite's
        # recursive_triggers is disabled. Keep conflict handling atomic/strict.
        if re.match(r'^(?:INSERT|UPDATE)\s+OR\b', statement, re.I):
            raise APIError(400, '写入 SQL 不支持 OR 冲突策略，请使用普通 INSERT/UPDATE')
        c = table._connection()
        view = self._view(table, c)
        changed, errors = [0], []

        def validate(index, value):
            name = table.columns[index]
            kind = table._types[name]
            try:
                if kind in _BOOL and value is not None:
                    if value not in (0, 1):
                        raise SchemaError(f'{name}: boolean requires 0 or 1')
                    value = bool(value)
                if kind in _OBJECT and value is not None:
                    if not isinstance(value, bytes):
                        raise SchemaError(f'{name}: structured fields use the row/import API')
                    value = table._msgpack.unpackb(value, raw=False, strict_map_key=False)
                result = self._coerce(table, {name: value})
                return table._encode(name, result[name])
            except Exception as exc:
                errors.append(exc)
                raise

        def written():
            changed[0] += 1
            return 0

        c.create_function('rtdb_validate', 2, validate)
        c.create_function('rtdb_written', 0, written)
        fields = ','.join(table._fields.values())
        values = ','.join(f'rtdb_validate({i},NEW."{name}")' for i, name in enumerate(table.columns))
        updates = ','.join(f'{physical}=rtdb_validate({i},NEW."{name}")'
                           for i, (name, physical) in enumerate(table._fields.items()))
        where = ' AND '.join(f'{table._fields[key]} IS OLD."{key}"' for key in table.primary_keys)
        changed_key = ' OR '.join(f'NEW."{key}" IS NOT OLD."{key}"' for key in table.primary_keys)
        protect_keys = f"SELECT RAISE(ABORT, 'KEY 主键不可修改，请新增或复制记录') WHERE {changed_key};"
        triggers = {
            'web_insert': f'INSERT INTO {table._data_sql} ({fields}) VALUES ({values});',
            'web_update': protect_keys + f'UPDATE {table._data_sql} SET {updates} WHERE {where};',
            'web_delete': f'DELETE FROM {table._data_sql} WHERE {where};',
        }
        for trigger, body in triggers.items():
            kind = trigger.split('_')[1].upper()
            c.execute(f'CREATE TEMP TRIGGER {trigger} INSTEAD OF {kind} ON {view} '
                      f'BEGIN {body} SELECT rtdb_written(); END')
        deadline = time.perf_counter() + 5
        count_triggers = ({f'{table._data_name}_count_insert', f'{table._data_name}_count_delete'}
                          if table.shared else {'count_insert', 'count_delete'})

        def authorizer(action, first, second, database, source):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() not in ('load_extension', 'writefile', 'readfile'):
                if (second or '').lower().startswith('rtdb_') and source not in triggers:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                if (database == 'temp' and first == table.name or
                        database == 'main' and first == table._data_name or
                        first == table._metadata_name and source in count_triggers):
                    return sqlite3.SQLITE_OK
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                if (database == 'temp' and first == table.name or
                        database == 'main' and first == table._data_name and source in triggers or
                        database == 'main' and first == table._metadata_name and source in count_triggers):
                    return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        def execute(connection):
            connection.set_authorizer(authorizer)
            connection.set_progress_handler(lambda: int(time.perf_counter() > deadline), 2000)
            try:
                cursor = connection.execute(statement)
                if cursor.description is not None:
                    raise APIError(400, '写入 SQL 暂不支持 RETURNING，请另行查询')
            except sqlite3.Error:
                if errors:
                    raise errors[0]
                raise
            finally:
                connection.set_authorizer(None)
                connection.set_progress_handler(None, 0)
            return dict(rows=[], rowcount=changed[0], readonly=False)

        return table._write(execute)

    def export(self, name, query):
        if query.get('format', 'json') not in ('json', 'csv'):
            raise APIError(400, '仅支持 JSON 或 CSV')
        with self.session(name) as table:
            with table._read_transaction() as c:
                rows, total = self._selection(table, c, query.get('where', ''), query.get('order', ''), self.max_rows)
            if total > self.max_rows:
                raise APIError(413, '导出结果超过 100,000 行，请增加过滤条件')
            if query.get('format', 'json') == 'json':
                return (json.dumps({'table': name, 'definition': table.definition.to_mapping(), 'rows': rows},
                                   ensure_ascii=False, indent=2).encode('utf-8'), 'application/json', name + '.json')
            stream = io.StringIO(newline='')
            writer = csv.writer(stream)
            writer.writerow(table.columns)
            for row in rows:
                cells = []
                for column in table.columns:
                    value = row[column]
                    if value is None:
                        cells.append(r'\N')
                    elif isinstance(value, (dict, list, bool)):
                        cells.append(json.dumps(value, ensure_ascii=False))
                    else:
                        value = str(value)
                        cells.append('\\' + value if value.startswith('\\') else value)
                writer.writerow(cells)
            return (stream.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8', name + '.csv')

    def import_data(self, name, payload):
        if not isinstance(payload.get('text'), str) or not payload['text'].strip():
            raise APIError(400, '导入内容不能为空')
        if payload.get('format') == 'json':
            data = json.loads(payload.get('text', ''))
            return self.insert(name, {'rows': data.get('rows') if isinstance(data, dict) else data})
        if payload.get('format') != 'csv':
            raise APIError(400, '仅支持 JSON 或 CSV')
        with self.session(name) as table:
            reader = csv.DictReader(io.StringIO(payload.get('text', '').lstrip('\ufeff')))
            if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)) or set(reader.fieldnames) - set(table.columns):
                raise APIError(422, 'CSV 列名缺失、重复或与表结构不符')
            rows = []
            for line, source in enumerate(reader, 2):
                if len(rows) >= self.max_rows:
                    raise APIError(413, '导入超过 100,000 行')
                row = {}
                for field, value in source.items():
                    if field is None or value is None:
                        raise APIError(422, f'CSV 第 {line} 行列数不符')
                    kind = table._types[field]
                    if value == r'\N':
                        row[field] = None
                    elif kind in _INT:
                        row[field] = int(value)
                    elif kind in _FLOAT:
                        row[field] = float(value)
                    elif kind in _BOOL | _OBJECT:
                        row[field] = json.loads(value)
                    else:
                        row[field] = value[1:] if value.startswith('\\\\') else value
                rows.append(self._coerce(table, row, complete=True))
            return {'inserted': table.insert_many(rows)}

    def save_schema(self, name, payload, *, creating=False, _already_locked=False):
        old_name = self.name(name)
        desired = TableDefinition.from_mapping(normalize_definition_labels(payload.get('definition')), self.schema_dir)
        new_name = self.name(desired.eng_name)
        if any(c.data_type.lower() not in _INT | _FLOAT | _BOOL | _TEXT | _OBJECT for c in desired.columns):
            raise APIError(422, '不支持的 SQLite 字段类型')
        gate = nullcontext() if _already_locked else self._gate
        lock = nullcontext() if _already_locked else _locked(self._schema_lock)
        with gate, lock:
            self._recover()
            if creating and payload.get('database_file'):
                database_file = payload.get('database_file')
                if not isinstance(database_file, str):
                    raise APIError(400, 'database_file 必须是 .sqlite3 文件名')
                database_name = database_file[:-len('.sqlite3')] if database_file.endswith('.sqlite3') else database_file
                database_name = self.name(database_name)
                grouped_schema = self.schema_dir / (database_name + '.json')
                grouped_db = self.data_dir / (database_name + '.sqlite3')
                if not grouped_db.is_file():
                    raise APIError(404, f'目标数据库文件不存在：{database_name}.sqlite3')
                if grouped_schema.is_file():
                    raw_document = _read_definition_json(grouped_schema)
                else:
                    embedded_document = load_embedded_database(grouped_db)
                    if embedded_document is None:
                        raise APIError(404, f'目标数据库缺少内置结构定义：{database_name}.sqlite3')
                    raw_document = embedded_document.to_mapping()
                if not (isinstance(raw_document, dict) and isinstance(raw_document.get('tables'), list)):
                    raise APIError(409, '只有多表数据库文件可以追加表结构')
                if any(isinstance(item, dict) and isinstance(item.get('head'), dict)
                       and item['head'].get('eng_name') == new_name for item in raw_document['tables']):
                    raise APIError(409, f'表名已存在：{new_name}')
                new_document = copy.deepcopy(raw_document)
                new_document['tables'].append(desired.to_mapping())
                atomic_json(grouped_schema, new_document)
                try:
                    with SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir) as db:
                        db[new_name]
                    atomic_text(grouped_schema.with_suffix('.py'), python_definition(new_document))
                except BaseException:
                    atomic_json(grouped_schema, raw_document)
                    atomic_text(grouped_schema.with_suffix('.py'), python_definition(raw_document))
                    raise
                return dict(name=new_name, definition=desired.to_mapping(),
                            schema_version=fingerprint(desired.to_mapping()))
            try:
                old_schema_path, old_document, old_database, old_grouped = self.definition_context(old_name)
                exists = True
            except APIError:
                old_schema_path, old_document, old_database, old_grouped = (
                    self.schema_dir / (old_name + '.json'), None, None, False)
                exists = False
            registry = SchemaRegistry(self.schema_dir)
            try:
                target_source = registry.source_path(new_name)
                target_exists = True
            except SchemaError:
                target_source = self.schema_dir / (new_name + '.json')
                target_exists = False
            embedded_target_exists = find_embedded_table(self.data_dir, new_name) is not None
            if creating and (target_exists or embedded_target_exists):
                raise APIError(409, '表名已存在')
            if creating and (self.data_dir / (new_name + '.sqlite3')).exists():
                raise APIError(409, f'数据库文件已存在但缺少可用的内置结构定义：{new_name}.sqlite3，请先加载 JSON 定义或恢复结构')
            if not creating and not exists:
                raise APIError(404, '表不存在')
            if not creating and new_name != old_name and (target_exists or any(
                    path.exists() for path in _sqlite_bundle(self.data_dir, new_name))):
                raise APIError(409, f'目标表名已存在：{new_name}')
            old = self.definition(old_name) if exists else None
            if old and payload.get('version') != fingerprint(old.to_mapping()):
                raise APIError(409, '表结构已变化，请刷新后修改')
            renames = _normalise_column_renames(old, desired, payload.get('column_renames')) if old else {}
            old_mapping = old.to_mapping() if old else None
            desired_mapping = desired.to_mapping()
            grouped = bool(old and old_grouped)
            if grouped:
                new_document = _replace_definition_table(old_document, old_name, desired_mapping)
                schema_path = old_schema_path
                database_path = self.data_dir / (old_schema_path.stem + '.sqlite3')
                physical_name = new_name
            else:
                new_document = desired_mapping
                schema_path = self.schema_dir / (new_name + '.json')
                database_path = self.data_dir / (new_name + '.sqlite3')
                physical_name = old_name
            pending = dict(name=old_name, old_name=old_name, new_name=new_name,
                           old=old_mapping, new=desired_mapping, python=True,
                           created=old is None)
            if grouped:
                pending.update(grouped=True, schema_path=str(schema_path), database_path=str(database_path),
                               physical_name=physical_name, old_document=old_document, new_document=new_document)
            atomic_json(self._journal, pending)
            try:
                if old is None:
                    atomic_json(schema_path, desired_mapping)
                    with SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir) as db:
                        db[new_name]
                else:
                    with SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir) as db:
                        table = db[old_name]
                        structural = [(c.eng_name, c.data_type, c.primary_key) for c in old.columns] != [
                            (c.eng_name, c.data_type, c.primary_key) for c in desired.columns]
                        def migrate(c):
                            if not structural:
                                desired.validate_rows(table._select(c))
                            else:
                                rows = _schema_rows(table, c, old, desired, renames)
                                try:
                                    import msgpack
                                except ImportError as error:
                                    if any(column.data_type.lower() in _OBJECT for column in desired.columns):
                                        raise APIError(422, '修改后包含结构化字段，但当前环境缺少 msgpack') from error
                                    msgpack = None
                                packer = table._msgpack or msgpack
                                rebuild_kwargs = {'table': table} if table.shared else {}
                                rebuild_schema_data(c, old, desired, rows,
                                                    lambda value, column: _encode_schema_value(value, column, packer),
                                                    **rebuild_kwargs)
                            c.execute(f'UPDATE {table._metadata_sql} SET fingerprint=? WHERE version=1', (fingerprint(desired_mapping),))
                            if new_name != old_name:
                                if grouped:
                                    _rename_shared_table(c, old_name, new_name)
                                remove_embedded_definition(c, old_name)
                            embedded_database_name = table.path.stem if grouped or new_name == old_name else new_name
                            write_embedded_definition(c, desired, embedded_database_name)
                        table._write(migrate)
                if new_name != old_name:
                    if not grouped:
                        _rename_sqlite_bundle(self.data_dir, old_name, new_name)
                atomic_json(schema_path, new_document)
                atomic_text(schema_path.with_suffix('.py'), python_definition(new_document))
                if new_name != old_name and not grouped:
                    old_schema_path.unlink(missing_ok=True)
                    old_schema_path.with_suffix('.py').unlink(missing_ok=True)
                self._journal.unlink()
            except BaseException:
                self._recover()
                raise
        return dict(name=new_name, definition=desired_mapping, schema_version=fingerprint(desired_mapping))

    def drop(self, name, payload):
        if payload.get('confirm') != name:
            raise APIError(400, '请输入表名确认删除')
        with self._gate, _locked(self._schema_lock):
            self._recover()
            with SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir) as db:
                definition = self.definition(name)
                table = db[name]
                if len(table):
                    raise APIError(409, '表有数据，请先导出并清空后删除表结构')
                if payload.get('version') != fingerprint(definition.to_mapping()):
                    raise APIError(409, '表结构已变化，请刷新')
                if table._connection().execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                    raise APIError(409, '数据库仍被其他进程使用')
            source = table.path
            schema_path, raw_document, parsed_document, grouped = self.definition_context(name)
            archive = self.data_dir / '.web-archive' / (name + '-' + uuid.uuid4().hex)
            archive.mkdir(parents=True)
            if not grouped:
                # Move the closed DB first; a live external Windows handle fails
                # here before removing its definition. Keep the empty table recoverable.
                os.replace(source, archive / source.name)
                moved = [source]
                try:
                    for sidecar in (schema_path, schema_path.with_suffix('.py')):
                        if sidecar.exists():
                            os.replace(sidecar, archive / sidecar.name)
                            moved.append(sidecar)
                except BaseException:
                    for sidecar in reversed(moved[1:]):
                        archived = archive / sidecar.name
                        if archived.exists():
                            os.replace(archived, sidecar)
                    os.replace(archive / source.name, source)
                    raise
                return {'deleted': True, 'archive': str(archive)}

            if len(parsed_document.tables) == 1:
                os.replace(source, archive / source.name)
                moved = [source]
                try:
                    for sidecar in (schema_path, schema_path.with_suffix('.py')):
                        if sidecar.exists():
                            os.replace(sidecar, archive / sidecar.name)
                            moved.append(sidecar)
                except BaseException:
                    for sidecar in reversed(moved[1:]):
                        archived = archive / sidecar.name
                        if archived.exists():
                            os.replace(archived, sidecar)
                    os.replace(archive / source.name, source)
                    raise
                return {'deleted': True, 'archive': str(archive)}

            # Removing one table from a shared file keeps all sibling tables and
            # rewrites the shared JSON/Python document atomically.
            shutil.copy2(source, archive / source.name)
            remaining = [table.to_mapping() for table in parsed_document.tables if table.eng_name != name]
            new_document = copy.deepcopy(raw_document)
            new_document['tables'] = remaining
            try:
                with SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir) as db:
                    table = db[name]
                    def remove(c):
                        c.execute(f'DROP TABLE {table._data_sql}')
                        c.execute(f'DROP TABLE {table._metadata_sql}')
                        remove_embedded_definition(c, name)
                    table._write(remove)
                atomic_json(schema_path, new_document)
                atomic_text(schema_path.with_suffix('.py'), python_definition(new_document))
            except BaseException:
                if source.exists():
                    source.unlink()
                shutil.copy2(archive / source.name, source)
                atomic_json(schema_path, raw_document)
                atomic_text(schema_path.with_suffix('.py'), python_definition(raw_document))
                raise
            return {'deleted': True, 'archive': str(archive), 'database_file': source.name,
                    'remaining_tables': [item['head']['eng_name'] for item in remaining]}

    def check(self, name):
        with self.session(name) as table:
            c = table._connection()
            integrity = [r[0] for r in c.execute('PRAGMA quick_check')]
            stored, counted = len(table), c.execute(f'SELECT COUNT(*) FROM {table._data_sql}').fetchone()[0]
            return dict(ok=integrity == ['ok'] and stored == counted, integrity=integrity,
                        row_count=stored, actual_count=counted, storage=table.storage_config())
