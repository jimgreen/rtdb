"""SQLite management operations. Each request owns and closes its connections.

The public row API uses logical names and primary keys, never grid positions.
Schema changes use a recovery journal bridging SQLite metadata and JSON files.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from core import SQLiteDatabase, TableDefinition, SchemaError
from core.file_mapped_db import _locked
from core.sqlite_db import _INT, _FLOAT, _BOOL, _TEXT, _OBJECT
from .server import APIError


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
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SQLiteManager:
    max_rows = 100000

    def __init__(self, data_dir, schema_dir, *, demo=False):
        self.data_dir = Path(data_dir).resolve()
        self.schema_dir = Path(schema_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.schema_dir.mkdir(parents=True, exist_ok=True)
        self.demo = demo
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
        name = self.name(pending['name'])
        path = self.data_dir / (name + '.sqlite3')
        actual = None
        if path.exists():
            with closing(sqlite3.connect(path)) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                    found = connection.execute('SELECT fingerprint FROM metadata WHERE version=1').fetchone()
                    actual = found[0] if found else None
        definition = pending['new'] if actual == fingerprint(pending['new']) else pending['old']
        if actual is not None and actual not in {fingerprint(pending['new']), fingerprint(pending['old'])}:
            raise APIError(409, '待恢复表的结构指纹不匹配，请检查结构变更日志')
        target = self.schema_dir / (name + '.json')
        if definition is None:
            target.unlink(missing_ok=True)
        else:
            atomic_json(target, definition)
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
                    durability='WAL / FULL', page_limit=500, export_limit=self.max_rows)

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
        triggers = {
            'web_insert': f'INSERT INTO data ({fields}) VALUES ({values});',
            'web_update': f'UPDATE data SET {updates} WHERE {where};',
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
        name = self.name(name)
        desired = TableDefinition.from_mapping(payload.get('definition'), self.schema_dir, name)
        if any(c.data_type.lower() not in _INT | _FLOAT | _BOOL | _TEXT | _OBJECT for c in desired.columns):
            raise APIError(422, '不支持的 SQLite 字段类型')
        with self._gate, _locked(self._schema_lock):
            self._recover()
            schema_path = self.schema_dir / (name + '.json')
            exists = schema_path.exists()
            if creating and (exists or (self.data_dir / (name + '.sqlite3')).exists()):
                raise APIError(409, '表名已存在')
            if not creating and not exists:
                raise APIError(404, '表不存在')
            old = self.definition(name) if exists else None
            if old and payload.get('version') != fingerprint(old.to_mapping()):
                raise APIError(409, '表结构已变化，请刷新后修改')
            pending = dict(name=name, old=old.to_mapping() if old else None, new=desired.to_mapping())
            atomic_json(self._journal, pending)
            try:
                if old is None:
                    atomic_json(schema_path, desired.to_mapping())
                    with SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir) as db:
                        db[name]
                else:
                    with SQLiteDatabase(self.data_dir, schema_directory=self.schema_dir) as db:
                        table = db[name]
                        structural = [(c.eng_name, c.data_type, c.primary_key) for c in old.columns] != [
                            (c.eng_name, c.data_type, c.primary_key) for c in desired.columns]
                        def migrate(c):
                            count = c.execute('SELECT row_count FROM metadata').fetchone()[0]
                            if structural and count:
                                raise APIError(409, '有数据时不允许增删、重排字段或改变类型/主键；请先导出备份并清空表')
                            if not structural:
                                desired.validate_rows(table._select(c))
                            else:
                                c.execute('DROP TABLE data')
                                parts = ['_pos INTEGER PRIMARY KEY AUTOINCREMENT']
                                for i, col in enumerate(desired.columns):
                                    kind = col.data_type.lower()
                                    affinity = 'INTEGER' if kind in _INT | _BOOL else 'REAL' if kind in _FLOAT else 'TEXT' if kind in _TEXT else 'BLOB'
                                    parts.append(f'c{i} {affinity}' + (' NOT NULL' if col.primary_key else ''))
                                c.execute('CREATE TABLE data (' + ','.join(parts) + ')')
                                c.execute('CREATE UNIQUE INDEX primary_key ON data (' + ','.join(f'c{i}' for i, col in enumerate(desired.columns) if col.primary_key) + ')')
                                for i, col in enumerate(desired.columns):
                                    if (col.eng_name,) != desired.primary_keys:
                                        c.execute(f'CREATE INDEX idx_c{i} ON data (c{i})')
                                c.execute('CREATE TRIGGER count_insert AFTER INSERT ON data BEGIN UPDATE metadata SET row_count=row_count+1; END')
                                c.execute('CREATE TRIGGER count_delete AFTER DELETE ON data BEGIN UPDATE metadata SET row_count=row_count-1; END')
                            c.execute('UPDATE metadata SET fingerprint=? WHERE version=1', (fingerprint(desired.to_mapping()),))
                        table._write(migrate)
                atomic_json(schema_path, desired.to_mapping())
                self._journal.unlink()
            except BaseException:
                self._recover()
                raise
        return dict(name=name, definition=desired.to_mapping(), schema_version=fingerprint(desired.to_mapping()))

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
            except BaseException:
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
