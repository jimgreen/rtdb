"""SQLite management operations. Each request owns and closes its connections.

The public row API uses logical names and primary keys, never grid positions.
Schema changes use a recovery journal bridging SQLite metadata and JSON files.
"""
from __future__ import annotations

import csv
import base64
import hashlib
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
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from core import SQLiteDatabase, TableDefinition, SchemaError
from core.file_mapped_db import _locked
from core.sqlite_db import _INT, _FLOAT, _BOOL, _TEXT, _OBJECT
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
    for raw in connection.execute(f'SELECT _pos,{fields} FROM data ORDER BY _pos'):
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


def rebuild_schema_data(connection, old, desired, rows=None, encoder=None):
    """Rebuild cN storage inside the caller's transaction, preserving row positions."""
    sequence = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='data'").fetchone()
    high_water = sequence[0] if sequence else 0
    parts = ['_pos INTEGER PRIMARY KEY AUTOINCREMENT']
    for i, column in enumerate(desired.columns):
        kind = column.data_type.lower()
        affinity = 'INTEGER' if kind in _INT | _BOOL else 'REAL' if kind in _FLOAT else 'TEXT' if kind in _TEXT else 'BLOB'
        parts.append(f'c{i} {affinity}' + (' NOT NULL' if column.primary_key else ''))
    connection.execute('CREATE TABLE data_schema_next (' + ','.join(parts) + ')')
    old_fields = {column.eng_name: f'c{i}' for i, column in enumerate(old.columns)}
    fields = ','.join(f'c{i}' for i in range(len(desired.columns)))
    if rows is None:
        sources = ','.join(old_fields.get(column.eng_name, 'NULL') for column in desired.columns)
        connection.execute(f'INSERT INTO data_schema_next (_pos,{fields}) SELECT _pos,{sources} FROM data')
    else:
        if encoder is None:
            raise ValueError('an encoder is required for converted schema rows')
        values = [(position, *(encoder(row.get(column.eng_name), column) for column in desired.columns))
                  for position, row in rows]
        connection.executemany(f'INSERT INTO data_schema_next (_pos,{fields}) VALUES (' +
                               ','.join('?' for _ in range(len(desired.columns) + 1)) + ')', values)
    connection.execute('DROP TABLE data')
    connection.execute('ALTER TABLE data_schema_next RENAME TO data')
    # Preserve deleted rows' high-water mark, including migrations of empty tables.
    connection.execute("DELETE FROM sqlite_sequence WHERE name='data'")
    connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('data',?)", (high_water,))
    connection.execute('CREATE UNIQUE INDEX primary_key ON data (' + ','.join(f'c{i}' for i, column in enumerate(desired.columns) if column.primary_key) + ')')
    for i, column in enumerate(desired.columns):
        if (column.eng_name,) != desired.primary_keys:
            connection.execute(f'CREATE INDEX idx_c{i} ON data (c{i})')
    # Copy before installing count triggers: metadata.row_count stays unchanged.
    connection.execute('CREATE TRIGGER count_insert AFTER INSERT ON data BEGIN UPDATE metadata SET row_count=row_count+1; END')
    connection.execute('CREATE TRIGGER count_delete AFTER DELETE ON data BEGIN UPDATE metadata SET row_count=row_count-1; END')


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
        if value.lower() in ('data', 'metadata') or value.lower().startswith('sqlite_'):
            raise APIError(400, '该表名由 SQLite 存储内核保留')
        return value

    def definition(self, name):
        path = self.schema_dir / (self.name(name) + '.json')
        if not path.is_file():
            raise APIError(404, f'表不存在：{name}')
        return TableDefinition.from_json_file(path)

    def _recover(self):
        if not self._journal.exists():
            return
        pending = json.loads(self._journal.read_text(encoding='utf-8'))
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
                    python_exists=python_path.is_file(), rtdb=False, selectable=False,
                    row_count=None, stored_fingerprint=None, definition_match=False,
                    status='未检查', error=None)
        try:
            with closing(sqlite3.connect(str(path))) as connection:
                metadata = connection.execute(
                    "SELECT row_count, fingerprint FROM metadata WHERE version=1"
                ).fetchone()
                if metadata is None:
                    item.update(status='不是 RTDB SQLite 文件', error='缺少 metadata 元数据')
                    return item
                row_count, stored_fingerprint = metadata
                if not isinstance(row_count, int) or row_count < 0 or not isinstance(stored_fingerprint, str):
                    raise sqlite3.DatabaseError('metadata 元数据无效')
                if not connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='data'"
                ).fetchone():
                    raise sqlite3.DatabaseError('缺少 data 数据表')
                actual_count = connection.execute('SELECT COUNT(*) FROM data').fetchone()[0]
                integrity = connection.execute('PRAGMA quick_check').fetchone()[0]
                if integrity != 'ok' or actual_count != row_count:
                    item.update(rtdb=True, row_count=row_count, stored_fingerprint=stored_fingerprint,
                                status='完整性检查失败', error=f'quick_check={integrity}, 记录数={actual_count}')
                    return item
                item.update(rtdb=True, row_count=row_count, stored_fingerprint=stored_fingerprint)
        except (OSError, sqlite3.Error) as error:
            item.update(status='无法读取', error=str(error))
            return item
        if not schema_path.is_file():
            item.update(status='缺少 JSON 定义', error=f'缺少 {schema_path.name}')
            return item
        try:
            definition = TableDefinition.from_json_file(schema_path)
            if definition.eng_name != name:
                item.update(status='定义表名不匹配', error=f'JSON 表名为 {definition.eng_name}')
                return item
            item['definition_match'] = fingerprint(definition.to_mapping()) == item['stored_fingerprint']
        except (OSError, SchemaError, json.JSONDecodeError) as error:
            item.update(status='JSON 定义无效', error=str(error))
            return item
        if not item['definition_match']:
            item.update(status='结构指纹不匹配', error='SQLite metadata 与 JSON 定义不一致')
            return item
        item.update(status='可用', selectable=True)
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

    def create_database_file(self, payload):
        if not isinstance(payload, dict):
            raise APIError(400, '新建数据库文件参数无效')
        name = self.name(payload.get('name', ''))
        label = normalize_definition_labels({'head': {'eng_name': name, 'chn_name': payload.get('chn_name')}})['head']['chn_name']
        if not isinstance(label, str) or not label.strip():
            raise APIError(400, '中文表名不能为空')
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
            if json_content is not None:
                try:
                    document = json.loads(json_content.decode('utf-8-sig'))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise APIError(422, f'{name}.json 不是有效的 UTF-8 JSON') from error
                try:
                    definition = TableDefinition.from_mapping(document, self.schema_dir, name)
                except SchemaError as error:
                    raise APIError(422, f'{name}.json 定义无效：{error}') from error
                if definition.eng_name != name:
                    raise APIError(422, f'{name}.json 的英文表名必须为 {name}')
            else:
                if not schema_path.is_file():
                    raise APIError(422, f'服务端缺少 {name}.json，请同时上传同名 JSON 定义')
                definition = self.definition(name)

            try:
                with closing(sqlite3.connect(str(stage_db))) as connection:
                    metadata = connection.execute(
                        'SELECT row_count, fingerprint FROM metadata WHERE version=1'
                    ).fetchone()
                    if metadata is None:
                        raise APIError(422, '上传文件缺少 RTDB metadata 元数据')
                    row_count, stored_fingerprint = metadata
                    if not connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='data'"
                    ).fetchone():
                        raise APIError(422, '上传文件缺少 RTDB data 数据表')
                    actual_count = connection.execute('SELECT COUNT(*) FROM data').fetchone()[0]
                    integrity = connection.execute('PRAGMA quick_check').fetchone()[0]
            except sqlite3.Error as error:
                raise APIError(422, f'上传文件不是有效的 SQLite 数据库：{error}') from error
            if integrity != 'ok' or actual_count != row_count:
                raise APIError(422, f'上传文件完整性检查失败：quick_check={integrity}，记录数={actual_count}，metadata={row_count}')
            if not isinstance(row_count, int) or row_count < 0 or row_count > self.max_rows:
                raise APIError(413, f'上传文件记录数超过 {self.max_rows} 条')
            if stored_fingerprint != fingerprint(definition.to_mapping()):
                raise APIError(409, 'SQLite metadata 与 JSON 定义结构指纹不一致')

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
                else:
                    paths.append(target_python)
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
                        atomic_json(target_json, definition.to_mapping())
                    atomic_text(target_python, python_definition(definition.to_mapping()))
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
                    'row_count': row_count, 'bytes': len(database_bytes), 'replaced': replaced,
                    'definition_updated': json_content is not None, 'python_updated': True}
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
        with self.session() as db:
            for name in sorted(db.schema_registry.names()):
                definition = self.definition(name)
                item = dict(name=name, definition=definition.to_mapping(), schema_version=fingerprint(definition.to_mapping()))
                try:
                    table = db[name]
                    item.update(row_count=len(table), bytes=table.path.stat().st_size, error=None)
                except (SchemaError, sqlite3.Error) as exc:
                    item.update(row_count=None, bytes=0, error=str(exc))
                result.append(item)
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
        connection.execute(f'CREATE TEMP VIEW IF NOT EXISTS {name} AS SELECT {columns} FROM main.data')
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
                    database == 'main' and first == 'data'):
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
                count = c.execute('SELECT row_count FROM metadata').fetchone()[0]
                if payload.get('expected_count') != count:
                    raise APIError(409, '记录数量已变化，请刷新后重试')
                return c.execute('DELETE FROM data').rowcount
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
            'web_insert': f'INSERT INTO data ({fields}) VALUES ({values});',
            'web_update': protect_keys + f'UPDATE data SET {updates} WHERE {where};',
            'web_delete': f'DELETE FROM data WHERE {where};',
        }
        for trigger, body in triggers.items():
            kind = trigger.split('_')[1].upper()
            c.execute(f'CREATE TEMP TRIGGER {trigger} INSTEAD OF {kind} ON {view} '
                      f'BEGIN {body} SELECT rtdb_written(); END')
        deadline = time.perf_counter() + 5

        def authorizer(action, first, second, database, source):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() not in ('load_extension', 'writefile', 'readfile'):
                if (second or '').lower().startswith('rtdb_') and source not in triggers:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                if (database == 'temp' and first == table.name or
                        database == 'main' and first == 'data' or
                        first == 'metadata' and source in ('capacity', 'count_insert', 'count_delete')):
                    return sqlite3.SQLITE_OK
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                if (database == 'temp' and first == table.name or
                        database == 'main' and first == 'data' and source in triggers or
                        database == 'main' and first == 'metadata' and source in ('count_insert', 'count_delete')):
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

    def save_schema(self, name, payload, *, creating=False):
        old_name = self.name(name)
        desired = TableDefinition.from_mapping(normalize_definition_labels(payload.get('definition')), self.schema_dir)
        new_name = self.name(desired.eng_name)
        if any(c.data_type.lower() not in _INT | _FLOAT | _BOOL | _TEXT | _OBJECT for c in desired.columns):
            raise APIError(422, '不支持的 SQLite 字段类型')
        with self._gate, _locked(self._schema_lock):
            self._recover()
            old_schema_path = self.schema_dir / (old_name + '.json')
            new_schema_path = self.schema_dir / (new_name + '.json')
            exists = old_schema_path.exists()
            if creating and (new_schema_path.exists() or (self.data_dir / (new_name + '.sqlite3')).exists()):
                raise APIError(409, '表名已存在')
            if not creating and not exists:
                raise APIError(404, '表不存在')
            if not creating and new_name != old_name and (new_schema_path.exists() or any(
                    path.exists() for path in _sqlite_bundle(self.data_dir, new_name))):
                raise APIError(409, f'目标表名已存在：{new_name}')
            old = self.definition(old_name) if exists else None
            if old and payload.get('version') != fingerprint(old.to_mapping()):
                raise APIError(409, '表结构已变化，请刷新后修改')
            renames = _normalise_column_renames(old, desired, payload.get('column_renames')) if old else {}
            pending = dict(name=old_name, old_name=old_name, new_name=new_name,
                           old=old.to_mapping() if old else None, new=desired.to_mapping(), python=True,
                           created=old is None)
            atomic_json(self._journal, pending)
            try:
                if old is None:
                    atomic_json(new_schema_path, desired.to_mapping())
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
                                rebuild_schema_data(c, old, desired, rows,
                                                    lambda value, column: _encode_schema_value(value, column, packer))
                            c.execute('UPDATE metadata SET fingerprint=? WHERE version=1', (fingerprint(desired.to_mapping()),))
                        table._write(migrate)
                if new_name != old_name:
                    _rename_sqlite_bundle(self.data_dir, old_name, new_name)
                atomic_json(new_schema_path, desired.to_mapping())
                atomic_text(new_schema_path.with_suffix('.py'), python_definition(desired.to_mapping()))
                if new_name != old_name:
                    old_schema_path.unlink(missing_ok=True)
                    old_schema_path.with_suffix('.py').unlink(missing_ok=True)
                self._journal.unlink()
            except BaseException:
                self._recover()
                raise
        return dict(name=new_name, definition=desired.to_mapping(), schema_version=fingerprint(desired.to_mapping()))

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
            archive = self.data_dir / '.web-archive' / (name + '-' + uuid.uuid4().hex)
            archive.mkdir(parents=True)
            # Move the closed DB first; a live external Windows handle fails
            # here before removing its definition. Keep the empty table recoverable.
            source = self.data_dir / (name + '.sqlite3')
            os.replace(source, archive / source.name)
            try:
                os.replace(self.schema_dir / (name + '.json'), archive / (name + '.json'))
                python_path = self.schema_dir / (name + '.py')
                if python_path.exists():
                    os.replace(python_path, archive / python_path.name)
            except BaseException:
                archived_json = archive / (name + '.json')
                if archived_json.exists():
                    os.replace(archived_json, self.schema_dir / archived_json.name)
                os.replace(archive / source.name, source)
                raise
            return {'deleted': True, 'archive': str(archive)}

    def check(self, name):
        with self.session(name) as table:
            c = table._connection()
            integrity = [r[0] for r in c.execute('PRAGMA quick_check')]
            stored, counted = len(table), c.execute('SELECT COUNT(*) FROM data').fetchone()[0]
            return dict(ok=integrity == ['ok'] and stored == counted, integrity=integrity,
                        row_count=stored, actual_count=counted, storage=table.storage_config())
