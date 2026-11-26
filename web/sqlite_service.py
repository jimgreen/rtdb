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
import tempfile
import time
import uuid
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from core import SQLiteDatabase, TableDefinition, SchemaError, SchemaRegistry, load_database_definition
from core.sqlite_db import _INT, _FLOAT, _BOOL, _TEXT, _OBJECT, _BLOB
from core.sqlite_schema import (DB_SUFFIX, _read_embedded_database, ensure_embedded_schema, find_embedded_table,
                                is_rtdb_database_file, load_embedded_database, native_database,
                                remove_embedded_definition, sqlite_filename, validate_native_table,
                                write_embedded_definition)
from core.definition_files import python_definition, normalize_definition_labels
from .audit import OperationAuditLog, error_summary, result_summary
from .errors import APIError
from . import write_receipts
from .tasks import checkpoint, cancellation_requested
from .sqlite_runtime import SQLiteRuntime


def fingerprint(value):
    def encode_native_blob(item):
        if isinstance(item, bytes):
            return {'__sqlite_blob__': item.hex()}
        raise TypeError(f'{type(item).__name__} is not JSON serializable')

    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=encode_native_blob).encode()).hexdigest()


def browser_value(value):
    """Keep signed 64-bit identifiers exact in JavaScript."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2 ** 53 - 1:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {k: browser_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [browser_value(v) for v in value]
    return value


def json_blob(value):
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f'{type(value).__name__} is not JSON serializable')


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


def _rename_shared_table(connection, old_name, new_name, desired=None):
    """Rename a logical table's physical objects inside a shared SQLite file."""
    old_data, new_data = f'rtdb_data_{old_name}', f'rtdb_data_{new_name}'
    old_meta, new_meta = f'rtdb_metadata_{old_name}', f'rtdb_metadata_{new_name}'
    quote = lambda value: '"' + value.replace('"', '""') + '"'
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (new_data,)).fetchone():
        raise APIError(409, f'目标表名已存在：{new_name}')
    for suffix in ('count_insert', 'count_delete'):
        connection.execute(f'DROP TRIGGER IF EXISTS {quote(old_data + "_" + suffix)}')
    # 删除旧表名对应的主键及辅助索引，防止残留僵尸索引或毒化旧表名。
    old_indexes = [
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (old_data,),
        ).fetchall()
    ]
    for index_name in old_indexes:
        connection.execute(f'DROP INDEX IF EXISTS {quote(index_name)}')
    connection.execute(f'ALTER TABLE {quote(old_data)} RENAME TO {quote(new_data)}')
    connection.execute(f'ALTER TABLE {quote(old_meta)} RENAME TO {quote(new_meta)}')
    if desired is not None:
        pk_cols = ','.join(f'c{i}' for i, column in enumerate(desired.columns) if column.primary_key)
        connection.execute(f'CREATE UNIQUE INDEX {quote(new_data + "_primary_key")} ON {quote(new_data)} ({pk_cols})')
    connection.execute(f'CREATE TRIGGER {quote(new_data + "_count_insert")} AFTER INSERT ON {quote(new_data)} '
                       f'BEGIN UPDATE {quote(new_meta)} SET row_count=row_count+1; END')
    connection.execute(f'CREATE TRIGGER {quote(new_data + "_count_delete")} AFTER DELETE ON {quote(new_data)} '
                       f'BEGIN UPDATE {quote(new_meta)} SET row_count=row_count-1; END')


def _sqlite_bundle(root, name):
    base = Path(root) / (name + DB_SUFFIX)
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


_DATABASE_FILE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\.db$')
_DATABASE_JSON_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\.json$')
_DATABASE_SIDE_FILES = ('-wal', '-shm')
_MAX_DATABASE_UPLOAD_BYTES = 16 * 1024 * 1024


def is_native_database(path):
    """Recognize explicitly registered external SQLite databases."""
    return sqlite_filename(path.name) and native_database(path)


def is_listed_database(path, schema_dir=None):
    return path.is_file() and (is_rtdb_database_file(path, schema_dir) or is_native_database(path))


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
        numeric = float(value)
        if not math.isfinite(numeric):
            raise APIError(422, f'字段 {column.eng_name} 不能保存非有限数值')
        return numeric
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
    from core.sqlite_indexes import reconcile_indexes
    reconcile_indexes(connection,data_name,desired)
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

    def __init__(self, data_dir, schema_dir, *, demo=False, strict_paths=False):
        self.data_dir = Path(data_dir).resolve()
        self.schema_dir = Path(schema_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.schema_dir.mkdir(parents=True, exist_ok=True)
        self.demo = demo
        self.strict_paths = strict_paths
        self.database_file = None
        self.native = False
        self.read_only = False
        self.audit_log = OperationAuditLog(self.data_dir)
        self._journal = self.schema_dir / '.sqlite-web-pending.json'
        self._runtime = SQLiteRuntime(self.data_dir, self.schema_dir)
        from .storage_maintenance import StorageMaintenance
        self.storage_maintenance = StorageMaintenance(self)
        with self.exclusive():
            self._recover()
            for journal in self.schema_dir.glob('.sqlite-web.*.pending.json'):
                scoped = copy.copy(self)
                raw_tag = journal.name[len('.sqlite-web.'):-len('.pending.json')]
                database_file = None
                try:
                    info = json.loads(journal.read_text(encoding='utf-8'))
                    if isinstance(info, dict):
                        if isinstance(info.get('database_file'), str):
                            database_file = info['database_file']
                        elif isinstance(info.get('database_path'), str):
                            database_file = Path(info['database_path']).name
                except Exception:
                    pass
                scoped.database_file = database_file or (
                    raw_tag if Path(raw_tag).suffix.lower() in ('.db', '.sqlite', '.sqlite3') else raw_tag + DB_SUFFIX
                )
                scoped._journal = journal
                scoped._recover()

    def close(self):
        self._runtime.close()

    @contextmanager
    def exclusive(self):
        with self._runtime.scope(self.database_file, exclusive=True):
            yield

    @contextmanager
    def global_exclusive(self):
        """整目录排他锁：用于文件级创建、覆盖、重建与删除，确保关闭全部 observer。"""
        with self._runtime.scope(None, exclusive=True):
            yield

    @contextmanager
    def read_scope(self):
        if self.native:
            with self._runtime.scope(self.database_file):
                yield
            return
        while True:
            with self._runtime.scope(self.database_file):
                if not self._journal.exists():
                    yield
                    return
            with self.exclusive():
                self._recover()

    @staticmethod
    def name(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
            raise APIError(400, '表名必须是英文标识符')
        if value.lower() in ('data', 'metadata') or value.lower().startswith(('sqlite_', 'rtdb_')):
            raise APIError(400, '该表名由 SQLite 存储内核保留')
        return value

    def _table_name(self, value):
        # Existing RTDB schemas still use ASCII identifiers. Accept Chinese
        # names for lookups so external SAMP tables can be opened as stored.
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*', value):
            if any('\u4e00' <= char <= '\u9fff' for char in value):
                return value
        return self.name(value)

    def for_database(self, filename):
        if not sqlite_filename(filename):
            raise APIError(400, '请选择有效的 SQLite 数据库文件')
        path = self.data_dir / filename
        if path.resolve() != path or not (path.is_file() or
                (self.schema_dir / (path.stem + '.json')).is_file()):
            raise APIError(404, f'数据库文件不存在：{filename}')
        native = is_native_database(path)
        if not native and (not _DATABASE_FILE_RE.fullmatch(filename) or (path.is_file() and not is_rtdb_database_file(path, self.schema_dir))):
            raise APIError(404, f'找不到带内置结构的外部数据库：{filename}')
        scoped = copy.copy(self)  # Request-local identity; locks/audit remain shared.
        scoped.database_file = filename
        scoped.native = native
        tag = path.stem if path.suffix.lower() == DB_SUFFIX else filename
        scoped._journal = self.schema_dir / ('.sqlite-web.' + tag + '.pending.json')
        return scoped

    def require_writable(self, *, name=None, path=None):
        if self.read_only:
            raise APIError(403, '数据库只读')
        # File-level replacement and legacy repair assume RTDB's physical
        # layout. Native records and schema have their own write paths.
        if path is not None and is_native_database(path):
            raise APIError(403, '此操作不能覆盖或重建原生 SQLite 数据库')

    def _database(self, database_file=None):
        return SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir,
                              database_file=database_file or self.database_file, native_writable=not self.read_only)

    def _registry(self):
        return SchemaRegistry(self.schema_dir,
            database_name=Path(self.database_file).stem if self.database_file else None)

    def _find(self, name):
        return find_embedded_table(self.data_dir, name, self.database_file)

    def databases(self):
        files = {p.name for p in self.data_dir.iterdir()
                 if p.resolve() == p and is_listed_database(p, self.schema_dir)}
        files.update(p.stem + DB_SUFFIX for p in self.schema_dir.glob('*.json')
                     if _DATABASE_FILE_RE.fullmatch(p.stem + DB_SUFFIX))
        items = []
        for filename in sorted(files, key=str.lower):
            try:
                document = load_embedded_database(self.data_dir / filename)
                if document is None:
                    document = next(iter(self.for_database(filename)._registry().database_definitions()), None)
                items.append({'filename': filename, 'label': document.chn_name if document else Path(filename).stem,
                              'table_count': len(document.tables) if document else 0,
                              'read_only': self.read_only,
                              'storage_format': document.storage_format if document else None,
                              'error': None if document else f'缺少 {filename} 内置结构定义'})
            except (SchemaError, sqlite3.Error, APIError) as error:
                items.append({'filename': filename, 'label': Path(filename).stem, 'table_count': 0, 'error': str(error)})
        return {'databases': items}

    def definition(self, name):
        name = self._table_name(name)
        embedded = self._find(name)
        if embedded is not None:
            return embedded[0]
        if self.database_file and load_embedded_database(self.data_dir / self.database_file) is not None:
            raise APIError(404, f'表不存在：{name}')
        try:
            return self._registry().load(name)
        except SchemaError as error:
            raise APIError(404, f'表不存在：{name}') from error

    def definition_source(self, name):
        name = self._table_name(name)
        embedded = self._find(name)
        if embedded is not None:
            return self.schema_dir / (embedded[1].path.stem + '.json')
        try:
            return self._registry().source_path(name)
        except SchemaError as error:
            raise APIError(404, f'表不存在：{name}') from error

    def definition_context(self, name):
        """Return the runtime definition context, preferring SQLite metadata."""
        name = self._table_name(name)
        embedded = self._find(name)
        if embedded is not None:
            _definition, document = embedded
            return (self.schema_dir / (document.path.stem + '.json'),
                    document.to_mapping(), document, document.is_grouped)
        source = self.definition_source(name)
        document = load_database_definition(source)
        return source, document.to_mapping(), document, document.is_grouped

    def database_path(self, name, *, fallback=None):
        """Resolve the SQLite file that owns a logical table."""
        embedded = self._find(self._table_name(name))
        if embedded is not None:
            return embedded[1].path
        try:
            source = self.definition_source(name)
            return self.data_dir / (source.stem + DB_SUFFIX)
        except APIError:
            return self.data_dir / ((fallback or name) + DB_SUFFIX)

    def _recover(self):
        if not self._journal.exists():
            return
        pending = json.loads(self._journal.read_text(encoding='utf-8'))
        if self.strict_paths:
            for key, directory, suffix in (('database_path', self.data_dir, DB_SUFFIX),
                                           ('schema_path', self.schema_dir, '.json')):
                if key in pending:
                    target = Path(pending[key])
                    if target.parent != directory or target.suffix != suffix or target.resolve() != target:
                        raise APIError(409, '恢复记录中的文件路径不属于当前数据或定义目录')
        if pending.get('kind') == 'snapshot-restore':
            from .database_snapshots import recover_definition
            recover_definition(self, pending)
            return
        if pending.get('kind') == 'definition-import':
            document = load_embedded_database(Path(pending['database_path']))
            if document is None:
                raise APIError(409, '导入恢复失败：SQLite 内置结构不存在')
            target = Path(pending['schema_path'])
            atomic_json(target, document.to_mapping())
            atomic_text(target.with_suffix('.py'), python_definition(document.to_mapping()))
            self._journal.unlink()
            return
        if pending.get('grouped'):
            schema_path = Path(pending['schema_path'])
            python_path = schema_path.with_suffix('.py')
            database_path = Path(pending['database_path'])
            old_fingerprint = fingerprint(pending['old'])
            new_fingerprint = fingerprint(pending['new'])
            old_name = pending['old_name']
            new_name = pending.get('new_name', pending.get('physical_name', old_name))
            # 同时检查新表名与旧表名指纹：若迁移失败回滚，物理表仍为 old_name，不能因新表不存在而锁死库。
            actual_new = _sqlite_table_fingerprint(database_path, new_name)
            actual_old = _sqlite_table_fingerprint(database_path, old_name) if old_name != new_name else actual_new
            if actual_new == new_fingerprint:
                atomic_json(schema_path, pending['new_document'])
                if pending.get('python'):
                    atomic_text(python_path, python_definition(pending['new_document']))
            elif actual_old == old_fingerprint:
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
        old_path = self.data_dir / (old_name + DB_SUFFIX)
        new_path = self.data_dir / (new_name + DB_SUFFIX)
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
    def session(self, name=None, *, writable=False):
        # Serializes schema changes with request connections in this service.
        # SQLite still coordinates independently opened external processes.
        if name is not None and not self.database_file:
            with self.read_scope():
                filename = self.database_path(self._table_name(name)).name
            with self.for_database(filename).session(name, writable=writable) as table:
                yield table
            return
        with self.read_scope():
            if writable:
                self.require_writable()
            with self._database() as db:
                if name is None:
                    yield db
                else:
                    name = self._table_name(name)
                    if name not in db._embedded_tables():
                        if self.database_file and load_embedded_database(self.data_dir / self.database_file) is not None:
                            raise APIError(404, f'表不存在：{name}')
                        try:
                            db.schema_registry.load(name)
                        except SchemaError as error:
                            raise APIError(404, f'表不存在：{name}') from error
                    yield db[name]

    def info(self):
        return dict(engine='sqlite', sqlite_version=sqlite3.sqlite_version,
                    data_dir=str(self.data_dir), schema_dir=str(self.schema_dir), demo=self.demo,
                    audit_log=str(self.audit_log.log_dir), audit_log_legacy=str(self.audit_log.legacy_path),
                    audit_log_config=str(self.audit_log.config_path),
                    log_retention_days=self.audit_log.config().get('retention_days', 0),
                    durability='WAL / FULL', page_limit=500,
                    export_limit=self.max_rows)

    def _database_file_snapshot(self, path, *, check_integrity=True):
        """Inspect one server-side SQLite file without registering it first."""
        name = path.stem
        schema_path = self.schema_dir / (name + '.json')
        python_path = self.schema_dir / (name + '.py')
        item = dict(name=name, filename=path.name, path=str(path), size_bytes=path.stat().st_size,
                    modified_at=_file_mtime(path), definition_exists=schema_path.is_file(),
                    embedded_definition_exists=False,
                    definition_source=None,
                    python_exists=python_path.is_file(), rtdb=False, selectable=False, read_only=False,
                    row_count=None, stored_fingerprint=None, definition_match=False, tables=[],
                    status='未检查', error=None)
        try:
            # 合并为单次只读连接，列表展示阶段跳过 O(N) 的 quick_check / COUNT(*) 全表扫描。
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
                connection.execute('BEGIN')
                document = _read_embedded_database(connection, path)
                if document is not None and document.storage_format == 'native':
                    item.update(embedded_definition_exists=True, definition_source='SQLite 内置结构',
                                storage_format='native', read_only=self.read_only)
                    if check_integrity:
                        integrity = connection.execute('PRAGMA quick_check').fetchone()[0]
                        if integrity != 'ok':
                            item.update(status='完整性检查失败', error=f'quick_check={integrity}')
                            return item
                    total = 0 if check_integrity else None
                    for definition in document.tables:
                        if check_integrity:
                            validate_native_table(connection, definition)
                            data = _sql_quote_name(definition.eng_name)
                            count = connection.execute(f'SELECT COUNT(*) FROM {data}').fetchone()[0]
                            total += count
                        else:
                            count = None
                        item['tables'].append(dict(name=definition.eng_name, row_count=count,
                            stored_fingerprint=fingerprint(definition.to_mapping()), definition_match=True))
                    item.update(status='可用（原生 SQLite）', selectable=True, rtdb=True,
                                row_count=total, definition_match=True)
                    return item
                if document is not None:
                    item['embedded_definition_exists'] = True
                    item['storage_format'] = 'rtdb'
                    item['definition_source'] = 'SQLite 内置结构'
                elif schema_path.is_file():
                    document = load_database_definition(schema_path)
                    item['definition_source'] = 'JSON 兼容定义'
                if document is None:
                    item.update(status='缺少内置结构定义', error=f'缺少 {path.name} 内置结构定义（旧文件可通过同名 JSON 首次加载迁移）')
                    return item
                shared = document.is_grouped
                definitions = {table.eng_name: table for table in document.tables}
                if not shared and name not in definitions:
                    item.update(status='定义表名不匹配', error=f'JSON 表名为 {next(iter(definitions), "未知")}')
                    return item
                if check_integrity:
                    integrity = connection.execute('PRAGMA quick_check').fetchone()[0]
                    if integrity != 'ok':
                        item.update(status='完整性检查失败', error=f'quick_check={integrity}')
                        return item
                existing_tables = {
                    row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                total = 0
                matches = True
                for table_name, definition in definitions.items():
                    data_name = 'data' if not shared else f'rtdb_data_{table_name}'
                    metadata_name = 'metadata' if not shared else f'rtdb_metadata_{table_name}'
                    if metadata_name not in existing_tables:
                        raise sqlite3.DatabaseError(f'缺少 {metadata_name} 元数据')
                    quote = lambda value: '"' + value.replace('"', '""') + '"'
                    metadata = connection.execute(f'SELECT row_count, fingerprint FROM {quote(metadata_name)} WHERE version=1').fetchone()
                    if metadata is None:
                        raise sqlite3.DatabaseError(f'缺少 {metadata_name} 元数据')
                    row_count, stored_fingerprint = metadata
                    if not isinstance(row_count, int) or row_count < 0 or not isinstance(stored_fingerprint, str):
                        raise sqlite3.DatabaseError(f'{metadata_name} 元数据无效')
                    if data_name not in existing_tables:
                        raise sqlite3.DatabaseError(f'缺少 {data_name} 数据表')
                    count_ok = True
                    if check_integrity:
                        actual_count = connection.execute(f'SELECT COUNT(*) FROM {quote(data_name)}').fetchone()[0]
                        count_ok = actual_count == row_count
                    table_match = count_ok and stored_fingerprint == fingerprint(definition.to_mapping())
                    matches = matches and table_match
                    total += row_count
                    item['tables'].append(dict(name=table_name, row_count=row_count,
                                               stored_fingerprint=stored_fingerprint,
                                               definition_match=table_match))
                item.update(rtdb=True, row_count=total, stored_fingerprint=None,
                            definition_match=matches)
        except (SchemaError, json.JSONDecodeError) as error:
            item.update(status='SQLite 内置结构无效' if item['embedded_definition_exists']
                        else '定义文件无效', error=str(error))
            return item
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
        for path in sorted(self.data_dir.iterdir(), key=lambda candidate: candidate.name.lower()):
            if path.resolve() != path or not is_listed_database(path, self.schema_dir):
                continue
            try:
                files.append(self._database_file_snapshot(path, check_integrity=False))
            except OSError as error:
                files.append({'name': path.stem, 'filename': path.name, 'path': str(path),
                              'size_bytes': 0, 'modified_at': None, 'definition_exists': False,
                              'python_exists': False, 'rtdb': False, 'selectable': False,
                              'row_count': None, 'stored_fingerprint': None, 'definition_match': False,
                              'status': '无法读取', 'error': str(error)})
        return {'files': files, 'data_dir': str(self.data_dir), 'schema_dir': str(self.schema_dir)}

    def select_database_file(self, payload):
        filename = payload.get('filename') if isinstance(payload, dict) else None
        scoped = self.for_database(filename)
        filename = scoped.database_file
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
            raise APIError(400, '请选择有效的 .db 数据库文件')
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
        self.require_writable(path=path)
        with self.global_exclusive():
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
        self.require_writable(path=path)
        with self.global_exclusive():
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
        self.require_writable(path=path)
        if not path.is_file():
            raise APIError(404, f'服务端文件不存在：{filename}')
        with self.global_exclusive():
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

    def create_database_file(self, payload, *, _already_locked=False):
        if not _already_locked:
            with self.global_exclusive():
                self._recover()
                return self.create_database_file(payload, _already_locked=True)
        if not isinstance(payload, dict):
            raise APIError(400, '新建数据库文件参数无效')
        name = self.name(payload.get('name', ''))
        label = normalize_definition_labels({'head': {'eng_name': name, 'chn_name': payload.get('chn_name')}})['head']['chn_name']
        if not isinstance(label, str) or not label.strip():
            raise APIError(400, '中文表名不能为空')
        grouped_values = payload.get('tables')
        if grouped_values is not None:
            if not isinstance(grouped_values, list):
                raise APIError(400, 'tables 必须是表结构数组')
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
            target_db = self.data_dir / (name + DB_SUFFIX)
            target_python = self.schema_dir / (name + '.py')
            if any(path.exists() for path in (target_json, target_db, target_python)):
                raise APIError(409, f'数据库文件已存在：{name}{DB_SUFFIX}')
            document = {'database': {'eng_name': name, 'chn_name': label.strip()},
                        'tables': [definition.to_mapping() for definition in definitions]}
            atomic_json(target_json, document)
            try:
                if not definitions:
                    with closing(sqlite3.connect(target_db)) as connection, connection:
                        ensure_embedded_schema(connection, name, label.strip())
                with self._database(name + DB_SUFFIX) as db:
                    for definition in definitions:
                        db[definition.eng_name]
                atomic_text(target_python, python_definition(document))
            except BaseException:
                target_json.unlink(missing_ok=True)
                target_db.unlink(missing_ok=True)
                for sidecar in ('-wal', '-shm', '-journal'):
                    (self.data_dir / (target_db.name + sidecar)).unlink(missing_ok=True)
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
                     'disp_type': '整数', 'disp_menu': ''},
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
        result = self.save_schema(name, {'definition': definition}, creating=True, _already_locked=True)
        return {'created': True, 'filename': name + DB_SUFFIX, **result}

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
            raise APIError(400, '必须且只能上传一个 .db 数据库文件')
        database_name, database_bytes = database_items[0]
        name = database_name[:-len(DB_SUFFIX)]
        expected = {name + '.json', database_name + '-wal', database_name + '-shm'}
        unknown = set(names) - {database_name} - expected
        if unknown:
            raise APIError(400, '只支持同名 .json、.db-wal 和 .db-shm 文件')
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
            if not definitions and not database_document.is_grouped:
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
                    if integrity != 'ok':
                        raise APIError(422, f'上传文件完整性检查失败：quick_check={integrity}')
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
            with self.global_exclusive():
                self._recover()
                replaced = target_db.exists() or target_json.exists()
                if replaced and payload.get('confirm') != name:
                    raise APIError(409, f'目标文件已存在，请输入 {name} 确认覆盖')
                self.require_writable(path=target_db)
                sync_external_definition = json_content is not None or (
                    embedded_source and (target_json.exists() or target_python.exists())
                )
                backup = Path(tempfile.mkdtemp(prefix='.rtdb-upload-backup-', dir=self.data_dir))
                paths = [target_db, *(self.data_dir / (database_name + suffix) for suffix in _DATABASE_SIDE_FILES)]
                if sync_external_definition:
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
                    if sync_external_definition:
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
        if self.database_file:
            path += ('&' if '?' in path else '?') + 'database=' + self.database_file
        return self.audit_log.append(category=category, operation=operation, status=actual_status,
                              http_status=http_status, method=method, path=path, table=table,
                              duration_ms=duration_ms, result=result, error=failure, database=self.database_file)

    def tables(self):
        if not self.database_file:
            result = []
            for database in self.databases()['databases']:
                filename = database['filename']
                if not database['error']:
                    result.extend(self.for_database(filename).tables()['tables'])
                else:
                    result.append(dict(name=Path(filename).stem, database_file=filename,
                        definition={'head': {'eng_name': Path(filename).stem}, 'columns': []},
                        row_count=None, error=database['error'], orphan=True, selectable=False))
            return {'tables': result}
        result = []
        known_database_files = set()
        if self.database_file and load_embedded_database(self.data_dir / self.database_file) is not None:
            known_database_files.add(self.database_file)
        with self.session() as db:
            embedded = db._embedded_tables()
            names = set(embedded)
            try:
                if not embedded and not known_database_files:
                    names.update(db.schema_registry.names())
            except SchemaError:
                # A malformed optional JSON file must not hide valid embedded
                # definitions or database files from the table index.
                pass
            for name in sorted(names):
                if name in embedded:
                    definition, document = embedded[name]
                    database_path = document.path
                else:
                    definition = db.schema_registry.load(name)
                    document = db.schema_registry.database_definition(name)
                    database_path = self.data_dir / (self.database_file or (document.eng_name + DB_SUFFIX))
                item = dict(name=name, definition=definition.to_mapping(), schema_version=fingerprint(definition.to_mapping()),
                            database_file=database_path.name, database_name=database_path.stem,
                            orphan=False, selectable=True)
                known_database_files.add(database_path.name)
                try:
                    table = db[name]
                    item.update(row_count=len(table), bytes=table.path.stat().st_size,
                                database_file=table.path.name, database_name=table.path.stem, error=None,
                                read_only=table.read_only, storage_format='native' if table.native else 'rtdb')
                    known_database_files.add(table.path.name)
                except (SchemaError, sqlite3.Error) as exc:
                    item.update(row_count=None, bytes=0, error=str(exc))
                result.append(item)
        for path in sorted(self.data_dir.glob('*' + DB_SUFFIX), key=lambda candidate: candidate.name.lower()):
            if self.database_file and path.name != self.database_file:
                continue
            if not _DATABASE_FILE_RE.fullmatch(path.name) or path.name in known_database_files or not is_rtdb_database_file(path, self.schema_dir):
                continue
            name = path.stem
            definition = {'head': {'chn_name': '缺少内置结构定义', 'eng_name': name}, 'columns': []}
            result.append(dict(name=name, definition=definition, schema_version=None,
                               row_count=None, bytes=path.stat().st_size, database_file=path.name,
                               database_name=name, error=f'缺少 {name}{DB_SUFFIX} 内置结构定义',
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
            elif kind in _BLOB and isinstance(value, str):
                if not re.fullmatch(r'(?:[0-9a-fA-F]{2})*', value):
                    raise APIError(422, f'{name} 必须为成对的十六进制字符')
                converted[name] = bytes.fromhex(value)
        return table._normalise_row(converted, complete=complete)

    @staticmethod
    def _key(table, key):
        if not isinstance(key, list) or len(key) != len(table.primary_keys):
            raise APIError(400, '主键参数数量与表定义不符')
        return SQLiteManager._coerce(table, dict(zip(table.primary_keys, key)))

    @staticmethod
    def _view(table, connection):
        if table.native:
            return f'main.{table._data_sql}'
        name = _sql_quote_name(table.name)
        columns = ','.join(f'{physical} AS {_sql_quote_name(logical)}' for logical, physical in table._fields.items())
        connection.execute(f'CREATE TEMP VIEW IF NOT EXISTS {name} AS SELECT {columns} FROM main.{table._data_sql}')
        return name

    @staticmethod
    def _row_count(table, connection):
        if table.native:
            sql = f'SELECT COUNT(*) FROM main.{table._data_sql}'
        else:
            sql = f'SELECT row_count FROM {table._metadata_sql} WHERE version=1'
        return connection.execute(sql).fetchone()[0]

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
        connection.set_progress_handler(lambda: int(time.perf_counter() > deadline or cancellation_requested()), 2000)
        try:
            yield
        except sqlite3.OperationalError:
            checkpoint('查询已中断')
            raise
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
            order = ','.join(_sql_quote_name(key) for key in table.primary_keys)
        source = f'SELECT * FROM {view}{clause}'
        # Both totals and page rows share the caller's snapshot. Counting must
        # not sort the entire result; unfiltered counts use the trigger-maintained total.
        total = None if where else self._row_count(table, connection)
        with self._reader(table, connection):
            if total is None:
                total = connection.execute(f'SELECT COUNT(*) FROM ({source}\n)').fetchone()[0]
            records = connection.execute(f'SELECT * FROM ({source} ORDER BY {order}\n) LIMIT ? OFFSET ?', (limit, offset)).fetchall()
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
        with self.session(name, writable=True) as table:
            prepared = [self._coerce(table, row, complete=True) for row in rows]
            from .write_receipts import write
            table.definition.validate_rows(prepared)
            return write(table, lambda c: {'inserted': table._insert(c,prepared)})

    def edit(self, name, payload):
        with self.session(name, writable=True) as table:
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
            from .write_receipts import write
            return write(table, lambda c: {'updated': update(c)})

    def delete_rows(self, name, payload):
        items = payload.get('records')
        if not isinstance(items, list) or not 1 <= len(items) <= 500:
            raise APIError(400, '请选择 1–500 条记录')
        with self.session(name, writable=True) as table:
            key_value = lambda values: values[0] if len(table.primary_keys) == 1 else tuple(values)

            def remove(c):
                wheres, lookup_keys, versions, seen = [], [], [], set()
                for item in items:
                    if not isinstance(item, dict):
                        raise APIError(400, 'records 中每项必须是对象')
                    where = self._key(table, item.get('key'))
                    key_tuple = tuple(
                        float(where[k]) if table._types.get(k) in _FLOAT and isinstance(where[k], (int, float)) else where[k]
                        for k in table.primary_keys
                    )
                    if key_tuple in seen:
                        raise APIError(400, '重复主键')
                    seen.add(key_tuple)
                    wheres.append(where)
                    lookup_keys.append(key_value([where[k] for k in table.primary_keys]))
                    versions.append(item.get('version'))
                # 批量读取待删记录完成版本校验，避免最多 500 次逐条 SELECT 往返。
                current_rows = table.kv._get_many(c, lookup_keys)
                for lookup_key, expected_version in zip(lookup_keys, versions):
                    current = current_rows.get(lookup_key)
                    if current is None or expected_version != fingerprint(current):
                        raise APIError(409, '所选记录已变化，删除已撤销，请刷新')
                return sum(table._change(c, 'delete', where, strict=True) for where in wheres)
            from .write_receipts import write
            return write(table, lambda c: {'deleted': remove(c)})

    def clear(self, name, payload):
        if payload.get('confirm') != name:
            raise APIError(400, '请输入完整表名确认清空')
        with self.session(name, writable=True) as table:
            def clear(c):
                count = self._row_count(table, c)
                if payload.get('expected_count') != count:
                    raise APIError(409, '记录数量已变化，请刷新后重试')
                return c.execute(f'DELETE FROM {table._data_sql}').rowcount
            return write_receipts.write(table, lambda c: {'deleted': clear(c)})

    def sql(self, name, payload):
        self._table_name(name)
        statement = payload.get('sql', '')
        if not isinstance(statement, str):
            raise APIError(400, 'sql 必须是字符串')
        # 剥离引号外 SQL 注释，防止通过注释绕过关键词及 OR 冲突策略检查。
        tokens = r"""'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\n]*|/\*[\s\S]*?\*/"""
        statement = re.sub(tokens, lambda match: ' ' if match[0].startswith(('--', '/*')) else match[0], statement)
        statement = statement.strip().removesuffix(';').strip()
        if not statement or len(statement) > 20000:
            raise APIError(400, '请输入 SQL（最多 20,000 字符）')
        kind = statement.split(None, 1)[0].upper()
        started = time.perf_counter()
        with self.session(name) as table:
            if kind in ('SELECT', 'WITH'):
                with table._read_transaction() as c:
                    self._view(table, c)
                    with self._reader(table, c):
                        cursor = c.execute(statement)
                        fields = [d[0] for d in cursor.description]
                        values = cursor.fetchmany(501)
                # Decode object/bool fields when directly selected; aggregates stay scalar.
                rows = []
                for values_row in values[:500]:
                    row = {}
                    for column, value in zip(fields, values_row):
                        col_kind = table._types.get(column)
                        if not table.native and col_kind in _BOOL and isinstance(value, int) and value in (0, 1):
                            value = bool(value)
                        elif col_kind in _OBJECT and isinstance(value, bytes):
                            try:
                                value = table._msgpack.unpackb(value, raw=False, strict_map_key=False)
                            except Exception:
                                value = value.hex()
                        elif isinstance(value, bytes):
                            value = value.hex()
                        row[column] = value
                    rows.append(row)
                result = dict(rows=rows, columns=fields, rowcount=len(rows), truncated=len(values) > 500, readonly=True)
            elif kind in ('INSERT', 'UPDATE', 'DELETE'):
                self.require_writable(name=name)
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
                if kind in _BLOB and value is not None and not isinstance(value, bytes):
                    raise SchemaError(f'{name}: SQL 中请使用十六进制 BLOB 字面值')
                result = self._coerce(table, {name: value})
                return table._encode(name, result[name])
            except Exception as exc:
                errors.append(exc)
                raise

        def written():
            changed[0] += 1
            return 0

        if table.native:
            return self._write_native_sql(table, statement, validate, errors)
        view = self._view(table, c)
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

        from .write_receipts import write
        return write(table, execute)

    @staticmethod
    def _write_native_sql(table, statement, validate, errors):
        """Use the application's table, its declared constraints and its triggers."""
        validation_triggers = {'rtdb_web_validate_insert', 'rtdb_web_validate_update'}
        blocked_functions = {'load_extension', 'writefile', 'readfile'}

        def execute(connection):
            objects = list(connection.execute("SELECT name, type FROM main.sqlite_master WHERE type IN ('table','trigger')"))
            tables = {name for name, kind in objects
                      if kind == 'table' and not name.lower().startswith(('sqlite_', 'rtdb_'))}
            application_triggers = {name for name, kind in objects if kind == 'trigger'}
            deadline = time.perf_counter() + 5
            validations = ','.join(f'rtdb_validate_native({i}, NEW.{table._fields[name]})'
                                   for i, name in enumerate(table.columns))

            def authorize(action, first, second, database, source):
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION:
                    function = (second or '').lower()
                    if function == 'rtdb_validate_native':
                        return sqlite3.SQLITE_OK if source in validation_triggers else sqlite3.SQLITE_DENY
                    if function not in blocked_functions:
                        return sqlite3.SQLITE_OK
                if database == 'main' and first in tables:
                    if action == sqlite3.SQLITE_READ:
                        return sqlite3.SQLITE_OK
                    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                        if source in application_triggers:
                            return sqlite3.SQLITE_OK
                        if source is None and first == table._data_name:
                            if action != sqlite3.SQLITE_UPDATE or second not in table.primary_keys:
                                return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            try:
                connection.create_function('rtdb_validate_native', 2, validate)
                # Validate values after SQLite applies the physical table's affinity
                # and defaults, without replacing existing application triggers.
                connection.execute(f'CREATE TEMP TRIGGER rtdb_web_validate_insert BEFORE INSERT '
                                   f'ON main.{table._data_sql} BEGIN SELECT {validations}; END')
                connection.execute(f'CREATE TEMP TRIGGER rtdb_web_validate_update BEFORE UPDATE '
                                   f'ON main.{table._data_sql} BEGIN SELECT {validations}; END')
                connection.set_authorizer(authorize)
                connection.set_progress_handler(lambda: int(time.perf_counter() > deadline or cancellation_requested()), 2000)
                cursor = connection.execute(statement)
                try:
                    if cursor.description is not None:
                        raise APIError(400, '写入 SQL 暂不支持 RETURNING，请另行查询')
                    return dict(rows=[], rowcount=cursor.rowcount, readonly=False)
                finally:
                    cursor.close()
            except sqlite3.Error:
                if errors:
                    raise errors[0]
                raise
            finally:
                connection.set_authorizer(None)
                connection.set_progress_handler(None, 0)
                for trigger in validation_triggers:
                    connection.execute(f'DROP TRIGGER IF EXISTS temp.{trigger}')
                connection.create_function('rtdb_validate_native', 2, None)

        return write_receipts.write(table, execute)

    def export_document(self, name, query=None):
        """直接返回 JSON 导出结构的字典对象，避免 backup 路径重复序列化与反序列化。"""
        query = query or {}
        with self.session(name) as table:
            with table._read_transaction() as c:
                rows, total = self._selection(table, c, query.get('where', ''), query.get('order', ''), self.max_rows)
            if total > self.max_rows:
                raise APIError(413, '导出结果超过 100,000 行，请增加过滤条件')
            return {'table': name, 'definition': table.definition.to_mapping(), 'rows': rows}

    def export(self, name, query):
        if query.get('format', 'json') not in ('json', 'csv'):
            raise APIError(400, '仅支持 JSON 或 CSV')
        with self.session(name) as table:
            raw_columns = query.get('columns')
            if query.get('format', 'json') == 'csv' and raw_columns:
                export_columns = raw_columns.split(',') if isinstance(raw_columns, str) else list(raw_columns)
                if not export_columns or set(export_columns) - set(table.columns):
                    raise APIError(400, '导出字段不存在')
            else:
                export_columns = list(table.columns)
            with table._read_transaction() as c:
                rows, total = self._selection(table, c, query.get('where', ''), query.get('order', ''), self.max_rows)
            if total > self.max_rows:
                raise APIError(413, '导出结果超过 100,000 行，请增加过滤条件')
            if query.get('format', 'json') == 'json':
                return (json.dumps({'table': name, 'definition': table.definition.to_mapping(), 'rows': rows},
                                   ensure_ascii=False, indent=2, default=json_blob).encode('utf-8'),
                        'application/json', name + '.json')
            stream = io.StringIO(newline='')
            writer = csv.writer(stream)
            writer.writerow(export_columns)
            for row in rows:
                cells = []
                for column in export_columns:
                    value = row[column]
                    if value is None:
                        cells.append(r'\N')
                    elif isinstance(value, (dict, list, bool)):
                        cells.append(json.dumps(value, ensure_ascii=False))
                    elif isinstance(value, bytes):
                        cells.append(value.hex())
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
        with self.session(name, writable=True) as table:
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
                    elif kind in _BOOL:
                        text_val = value.strip().lower()
                        if text_val not in ('true', 'false', '0', '1'):
                            raise APIError(422, f'CSV 第 {line} 行字段 {field} 必须为布尔值（true/false 或 0/1）')
                        row[field] = text_val in ('true', '1')
                    elif kind in _OBJECT:
                        row[field] = json.loads(value)
                    else:
                        row[field] = value[1:] if value.startswith('\\\\') else value
                rows.append(self._coerce(table, row, complete=True))
            table.definition.validate_rows(rows)
            return write_receipts.write(table, lambda c: {'inserted': table._insert(c, rows)})

    def _promote_single_table(self, filename, table_name):
        """Keep an existing file and its rows while moving to per-table storage."""
        with self._database(filename) as database:
            existing = database[table_name]
            if existing.shared:
                return
            def promote(connection):
                data_name = 'rtdb_data_' + existing.name
                meta_name = 'rtdb_metadata_' + existing.name
                connection.execute(f'ALTER TABLE data RENAME TO "{data_name}"')
                connection.execute(f'ALTER TABLE metadata RENAME TO "{meta_name}"')
                objects = connection.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (data_name,)).fetchall()
                from .atomic_import import rename_ddl_object
                for kind, old_object, sql in objects:
                    connection.execute(f'DROP {kind} "{old_object}"')
                    connection.execute(rename_ddl_object(sql, kind, old_object, data_name + '_' + old_object))
            existing._write(promote)

    def _native_schema_scope(self, name, payload, *, creating=False):
        if self.database_file:
            return self if self.native else None
        if creating:
            filename = payload.get('database_file')
            if isinstance(filename, str) and '.' not in filename:
                filename += DB_SUFFIX
            if sqlite_filename(filename):
                path = self.data_dir / filename
                if path.resolve() == path and is_native_database(path):
                    return self.for_database(filename)
            return None
        embedded = self._find(self._table_name(name))
        if embedded is not None and embedded[1].storage_format == 'native':
            return self.for_database(embedded[1].path.name)
        return None

    def save_schema(self, name, payload, *, creating=False, _already_locked=False):
        native = self._native_schema_scope(name, payload, creating=creating)
        if native is not None:
            from .native_schema_edits import save_native_schema
            return save_native_schema(native, name, payload, creating=creating, already_locked=_already_locked)
        self.require_writable(name=name)
        if creating and self.database_file:
            payload = {**payload, 'database_file': self.database_file}
        old_name = self.name(name)
        desired = TableDefinition.from_mapping(normalize_definition_labels(payload.get('definition')), self.schema_dir)
        new_name = self.name(desired.eng_name)
        if any(c.data_type.lower() not in _INT | _FLOAT | _BOOL | _TEXT | _OBJECT for c in desired.columns):
            raise APIError(422, '不支持的 SQLite 字段类型')
        gate = nullcontext() if _already_locked else self.exclusive()
        with gate:
            self._recover()
            if creating and payload.get('database_file'):
                database_file = payload.get('database_file')
                if not isinstance(database_file, str):
                    raise APIError(400, 'database_file 必须是 .db 文件名')
                database_name = database_file[:-len(DB_SUFFIX)] if database_file.endswith(DB_SUFFIX) else database_file
                database_name = self.name(database_name)
                grouped_schema = self.schema_dir / (database_name + '.json')
                grouped_db = self.data_dir / (database_name + DB_SUFFIX)
                self.require_writable(path=grouped_db)
                if not grouped_db.is_file():
                    raise APIError(404, f'目标数据库文件不存在：{database_name}{DB_SUFFIX}')
                embedded_document = load_embedded_database(grouped_db)
                raw_document = embedded_document.to_mapping() if embedded_document else _read_definition_json(grouped_schema)
                if not (isinstance(raw_document, dict) and isinstance(raw_document.get('tables'), list)):
                    raise APIError(409, '只有多表数据库文件可以追加表结构')
                if any(isinstance(item, dict) and isinstance(item.get('head'), dict)
                       and item['head'].get('eng_name') == new_name for item in raw_document['tables']):
                    raise APIError(409, f'表名已存在：{new_name}')
                new_document = copy.deepcopy(raw_document)
                new_document['tables'].append(desired.to_mapping())
                if embedded_document and not embedded_document.is_grouped:
                    self._promote_single_table(database_name + DB_SUFFIX, embedded_document.tables[0].eng_name)
                if self.database_file:
                    from .schema_operations import create
                    return create(self,desired)
                atomic_json(grouped_schema, new_document)
                try:
                    with self._database(database_name + DB_SUFFIX) as db:
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
            registry = self._registry()
            try:
                registry.source_path(new_name)
                target_exists = True
            except SchemaError:
                target_exists = False
            embedded_target_exists = self._find(new_name) is not None
            if self.database_file and old_grouped:
                target_exists = embedded_target_exists
            if creating and (target_exists or embedded_target_exists):
                raise APIError(409, '表名已存在')
            if creating and (self.data_dir / (new_name + DB_SUFFIX)).exists():
                raise APIError(409, f'数据库文件已存在但缺少可用的内置结构定义：{new_name}{DB_SUFFIX}，请先加载 JSON 定义或恢复结构')
            if not creating and not exists:
                raise APIError(404, '表不存在')
            if not creating and new_name != old_name and (target_exists or (not old_grouped and not self.database_file and any(
                    path.exists() for path in _sqlite_bundle(self.data_dir, new_name)))):
                raise APIError(409, f'目标表名已存在：{new_name}')
            old = self.definition(old_name) if exists else None
            if old and payload.get('version') != fingerprint(old.to_mapping()):
                raise APIError(409, '表结构已变化，请刷新后修改')
            renames = _normalise_column_renames(old, desired, payload.get('column_renames')) if old else {}
            if old and self.database_file and not old_grouped:
                self._promote_single_table(self.database_file, old_name)
                old_grouped = True
                old_database = load_embedded_database(self.data_dir / self.database_file)
                old_document = old_database.to_mapping()
            old_mapping = old.to_mapping() if old else None
            desired_mapping = desired.to_mapping()
            database_label = old_database.chn_name if old_database is not None else None
            grouped = bool(old and old_grouped)
            if grouped:
                new_document = _replace_definition_table(old_document, old_name, desired_mapping)
                schema_path = old_schema_path
                database_path = self.data_dir / (old_schema_path.stem + DB_SUFFIX)
                physical_name = new_name
            else:
                new_document = desired_mapping
                schema_path = self.schema_dir / (new_name + '.json')
                database_path = self.data_dir / (new_name + DB_SUFFIX)
                physical_name = old_name
            pending = dict(name=old_name, old_name=old_name, new_name=new_name,
                           old=old_mapping, new=desired_mapping, python=True,
                           created=old is None, database_file=database_path.name)
            if grouped:
                pending.update(grouped=True, schema_path=str(schema_path), database_path=str(database_path),
                               physical_name=physical_name, old_document=old_document, new_document=new_document)
            atomic_json(self._journal, pending)
            try:
                if old is None:
                    atomic_json(schema_path, desired_mapping)
                    with self._database(database_path.name) as db:
                        db[new_name]
                else:
                    with self._database(old_schema_path.stem + DB_SUFFIX) as db:
                        table = db[old_name]
                        structural = [(c.eng_name, c.data_type, c.primary_key) for c in old.columns] != [
                            (c.eng_name, c.data_type, c.primary_key) for c in desired.columns]
                        def migrate(c):
                            if structural:
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
                                    _rename_shared_table(c, old_name, new_name, desired)
                                remove_embedded_definition(c, old_name)
                            embedded_database_name = table.path.stem if grouped or new_name == old_name else new_name
                            write_embedded_definition(c, desired, embedded_database_name, database_label)
                            from core.sqlite_indexes import reconcile_indexes
                            reconcile_indexes(c,'rtdb_data_'+new_name if table.shared else 'data',desired)
                            return dict(name=new_name,definition=desired_mapping,schema_version=fingerprint(desired_mapping))
                        write_receipts.write(table,migrate)
                if new_name != old_name:
                    if not grouped:
                        _rename_sqlite_bundle(self.data_dir, old_name, new_name)
                atomic_json(schema_path, new_document)
                atomic_text(schema_path.with_suffix('.py'), python_definition(new_document))
                if new_name != old_name and not grouped:
                    old_schema_path.unlink(missing_ok=True)
                    old_schema_path.with_suffix('.py').unlink(missing_ok=True)
                self._journal.unlink()
            except Exception:
                committed=write_receipts.committed_result()
                if committed is not None:
                    try:self._recover();return committed
                    except Exception as recovery_error:
                        return {**committed,'export_warning':f'SQLite 已提交，定义文件导出待恢复：{recovery_error}'}
                self._recover()
                raise
        return dict(name=new_name, definition=desired_mapping, schema_version=fingerprint(desired_mapping))

    def drop(self, name, payload):
        native = self._native_schema_scope(name, payload)
        if native is not None:
            from .native_schema_edits import drop_native_schema
            return drop_native_schema(native, name, payload)
        self.require_writable(name=name)
        if payload.get('confirm') != name:
            raise APIError(400, '请输入表名确认删除')
        if self.database_file:
            from .schema_operations import drop
            return drop(self,name,payload)
        with self.exclusive():
            self._recover()
            with self._database() as db:
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
            if (not grouped or len(parsed_document.tables) == 1) and not self.database_file:
                # 连同 -wal/-shm 伴随文件一起归档，避免残留 WAL 破坏后续新建同名库。
                os.replace(source, archive / source.name)
                moved = [source]
                try:
                    for sidecar in (
                        Path(str(source) + '-wal'), Path(str(source) + '-shm'),
                        schema_path, schema_path.with_suffix('.py'),
                    ):
                        if sidecar.exists():
                            os.replace(sidecar, archive / sidecar.name)
                            moved.append(sidecar)
                except BaseException:
                    for sidecar in reversed(moved):
                        archived = archive / sidecar.name
                        if archived.exists():
                            os.replace(archived, sidecar)
                    raise
                return {'deleted': True, 'archive': str(archive)}

            # Removing one table from a shared file keeps all sibling tables and
            # rewrites the shared JSON/Python document atomically.
            for bundle_file in _sqlite_bundle(source.parent, source.stem):
                if bundle_file.exists():
                    shutil.copy2(bundle_file, archive / bundle_file.name)
            remaining = [table.to_mapping() for table in parsed_document.tables if table.eng_name != name]
            new_document = copy.deepcopy(raw_document)
            new_document['tables'] = remaining
            try:
                with self._database() as db:
                    table = db[name]
                    def remove(c):
                        c.execute(f'DROP TABLE {table._data_sql}')
                        c.execute(f'DROP TABLE {table._metadata_sql}')
                        remove_embedded_definition(c, name)
                    table._write(remove)
                atomic_json(schema_path, new_document)
                atomic_text(schema_path.with_suffix('.py'), python_definition(new_document))
            except BaseException:
                for bundle_file in _sqlite_bundle(source.parent, source.stem):
                    bundle_file.unlink(missing_ok=True)
                    archived_file = archive / bundle_file.name
                    if archived_file.exists():
                        shutil.copy2(archived_file, bundle_file)
                atomic_json(schema_path, raw_document)
                atomic_text(schema_path.with_suffix('.py'), python_definition(raw_document))
                raise
            return {'deleted': True, 'archive': str(archive), 'database_file': source.name,
                    'remaining_tables': [item['head']['eng_name'] for item in remaining]}

    def check(self, name):
        with self.session(name) as table:
            # 放在同一读事务快照中校验，避免并发提交导致 stored != counted 误报。
            with table._read_transaction() as c:
                integrity = [r[0] for r in c.execute('PRAGMA quick_check')]
                stored = table._row_count(c)
                counted = c.execute(f'SELECT COUNT(*) FROM {table._data_sql}').fetchone()[0]
            return dict(ok=integrity == ['ok'] and stored == counted, integrity=integrity,
                        row_count=stored, actual_count=counted, storage=table.storage_config())
