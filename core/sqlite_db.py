"""SQLite-backed relation, list, key-value and SQL interfaces.

One schema-defined table per .sqlite3 file, WAL + synchronous=FULL, a connection
per thread, and native scalar columns/indexes. SQLite serializes writers across
processes. Transactions return only after COMMIT; reads do not cache rows.
Structured fields optionally use MessagePack BLOBs, never pickle or row JSON.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import multiprocessing as mp
from multiprocessing.managers import BaseManager
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping, MutableMapping, MutableSequence
from contextlib import contextmanager

from .file_mapped_db import (
    FileMappedDatabase, Table as _SQLTable, _compile_where, _parse_equality_mapping,
    _parse_order_by, _sort_rows, _unquote_identifier, _validate_identifier,
)
from .table_schema import SchemaError, SchemaRegistry

__all__ = ['SQLiteDatabase', 'SQLiteTable', 'SQLiteTableKeyValue',
           'SQLiteTableReadPipeline', 'SQLiteTableWriteBatch', 'SQLiteStorageError',
           'SQLiteBusyError', 'SQLiteWriteProcess', 'SQLiteWriteClient', 'SQLiteWriteProcessError',
           ]


class SQLiteStorageError(RuntimeError):
    """Backend lifecycle/configuration error."""


class SQLiteBusyError(SQLiteStorageError):
    """Another writer held the database beyond the configured retry budget."""


class SQLiteWriteProcessError(SQLiteStorageError):
    """The dedicated writer process could not complete a queued request."""


_INT = {'int', 'integer', 'long'}
_FLOAT = {'float', 'double', 'number', 'decimal'}
_BOOL = {'bool', 'boolean'}
_TEXT = {'str', 'string', 'text', 'varchar', 'char', 'date', 'datetime', 'time'}
_OBJECT = {'list', 'array', 'dict', 'object', 'json'}


class SQLiteTable(MutableSequence):
    def __init__(self, database, definition):
        self.database = database
        self.definition = self.schema = definition
        self.name = definition.eng_name
        self.columns = definition.column_names
        self.primary_keys = definition.primary_keys
        self.path = database.directory / (self.name + '.sqlite3')
        self._fields = {name: f'c{i}' for i, name in enumerate(self.columns)}
        self._types = {c.eng_name: c.data_type.strip().lower() for c in definition.columns}
        self._msgpack = None
        if any(kind in _OBJECT for kind in self._types.values()):
            try:
                import msgpack
            except ImportError as exc:
                raise ImportError('install msgpack>=1 for SQLite list/object/json columns') from exc
            self._msgpack = msgpack
        for kind in self._types.values():
            if kind not in _INT | _FLOAT | _BOOL | _TEXT | _OBJECT:
                raise SchemaError(f'unsupported SQLite data_type: {kind}')
        self._local = threading.local()
        self._connections = []
        self._connection_lock = threading.RLock()
        self._closed = False
        fingerprint = hashlib.sha256(json.dumps(definition.to_mapping(), sort_keys=True,
                                                ensure_ascii=False).encode()).hexdigest()
        def initialize(c):
            c.execute('CREATE TABLE IF NOT EXISTS metadata (version INTEGER PRIMARY KEY CHECK(version=1), '
                      'fingerprint TEXT NOT NULL, row_count INTEGER NOT NULL)')
            old = c.execute('SELECT fingerprint FROM metadata WHERE version=1').fetchone()
            if old:
                if old[0] != fingerprint:
                    raise SchemaError(f'SQLite {self.name}: schema differs from stored definition')
                return
            parts = ['_pos INTEGER PRIMARY KEY AUTOINCREMENT']
            for field, column in self._fields.items():
                kind = self._types[field]
                typ = ('INTEGER' if kind in _INT | _BOOL else 'REAL' if kind in _FLOAT
                       else 'TEXT' if kind in _TEXT else 'BLOB')
                parts.append(column + ' ' + typ + (' NOT NULL' if field in self.primary_keys else ''))
            c.execute('CREATE TABLE data (' + ','.join(parts) + ')')
            c.execute('CREATE UNIQUE INDEX primary_key ON data (' +
                      ','.join(self._fields[k] for k in self.primary_keys) + ')')
            for name, col in self._fields.items():
                if (name,) != self.primary_keys:
                    c.execute(f'CREATE INDEX idx_{col} ON data ({col})')
            c.execute('INSERT INTO metadata VALUES (1,?,0)', (fingerprint,))
            c.execute('CREATE TRIGGER count_insert AFTER INSERT ON data BEGIN '
                      'UPDATE metadata SET row_count=row_count+1; END')
            c.execute('CREATE TRIGGER count_delete AFTER DELETE ON data BEGIN '
                      'UPDATE metadata SET row_count=row_count-1; END')
        try:
            self._write(initialize)
        except BaseException:
            self.close()
            raise

    def _connection(self):
        self.database._check_open()
        with self._connection_lock:
            if self._closed:
                raise SQLiteStorageError('table is closed')
            connection = getattr(self._local, 'connection', None)
            if connection is None:
                connection = sqlite3.connect(self.path, timeout=self.database.busy_timeout,
                                             isolation_level=None, check_same_thread=False)
                try:
                    mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
                    if mode.lower() != 'wal':
                        raise SQLiteStorageError('WAL mode unavailable')
                    connection.execute('PRAGMA synchronous=FULL')
                    connection.execute(f'PRAGMA mmap_size={self.database.mmap_size}')
                    connection.execute('PRAGMA cache_size=-8192')
                    connection.execute('PRAGMA foreign_keys=ON')
                except BaseException:
                    connection.close()
                    raise
                self._local.connection = connection
                self._connections.append(connection)
            return connection

    def _write(self, callback):
        c = self._connection()
        if c.in_transaction:
            raise SQLiteStorageError('nested transactions are not supported')
        # Retry lock acquisition only. Never replay a callback or an ambiguous
        # COMMIT after an I/O failure; callbacks may have external side effects.
        for attempt in range(self.database.transaction_retries):
            try:
                c.execute('BEGIN IMMEDIATE')
                break
            except sqlite3.OperationalError as exc:
                code = getattr(exc, 'sqlite_errorcode', 0) & 255
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                if attempt + 1 == self.database.transaction_retries:
                    raise SQLiteBusyError('SQLite writer timeout') from exc
                time.sleep(min(.005 * 2**attempt, .05))
        try:
            result = callback(c)
            c.commit()
            return result
        except sqlite3.IntegrityError as exc:
            c.rollback()
            raise SchemaError(str(exc)) from exc
        except BaseException:
            c.rollback()
            raise

    @contextmanager
    def _read_transaction(self):
        c = self._connection()
        if c.in_transaction:
            raise SQLiteStorageError('nested reads inside callbacks are not supported')
        c.execute('BEGIN')
        try:
            yield c
        finally:
            c.rollback()

    def storage_config(self):
        c = self._connection()
        return {name: c.execute('PRAGMA ' + name).fetchone()[0] for name in
                ('journal_mode', 'synchronous', 'mmap_size', 'busy_timeout', 'wal_autocheckpoint')}

    def _encode(self, name, value):
        if value is None:
            return None
        kind = self._types[name]
        if kind in _OBJECT:
            return self._msgpack.packb(value, use_bin_type=True)
        if kind in _FLOAT:
            return float(value)
        if kind in _BOOL:
            return int(bool(value))
        if kind in _INT and not -(2**63) <= value < 2**63:
            raise SchemaError(f'{name}: integer outside SQLite signed 64-bit range')
        if isinstance(value, float) and not math.isfinite(value):
            raise SchemaError(f'{name}: non-finite number')
        return value

    def _normalise_row(self, row, *, complete=False):
        if not isinstance(row, Mapping):
            raise TypeError('rows/patches must be mappings')
        row = copy.deepcopy(dict(row))
        unknown = set(row) - set(self.columns)
        if unknown:
            raise SchemaError(f'unknown columns: {unknown}')
        if complete:
            self.definition.validate_rows([row])
            row = {name: row.get(name) for name in self.columns}
        for name, value in row.items():
            if value is not None:
                self.definition._validate_value(value, self.definition.column(name), 1)
            elif name in self.primary_keys:
                raise SchemaError(f'primary key {name} cannot be null')
            self._encode(name, value)
        return row

    def _decode(self, record, fields=None):
        row = {}
        for name, value in zip(fields or self.columns, record):
            if value is not None:
                if self._types[name] in _BOOL:
                    value = bool(value)
                elif self._types[name] in _FLOAT:
                    value = float(value)
                elif self._types[name] in _OBJECT:
                    value = self._msgpack.unpackb(value, raw=False, strict_map_key=False)
            row[name] = value
        return row

    def _where_sql(self, where):
        eq = where if isinstance(where, Mapping) else _parse_equality_mapping(where)
        if where is not None and eq is None:
            return None
        terms, values = [], []
        for name, value in (eq or {}).items():
            if name not in self._fields or self._types[name] in _OBJECT:
                return None
            kind = self._types[name]
            if value is not None:
                if kind in _TEXT and not isinstance(value, str):
                    return '0', []
                if kind in _INT | _FLOAT | _BOOL and not isinstance(value, (int, float)):
                    return '0', []
                if isinstance(value, int) and not -(2**63) <= value < 2**63:
                    return None
            terms.append(self._fields[name] + ' IS ?')
            values.append(value)
        return ' AND '.join(terms) or '1', values

    @staticmethod
    def _window(limit, offset=0):
        if not isinstance(offset, int) or offset < 0 or (limit is not None and
                (not isinstance(limit, int) or limit < 0)):
            raise ValueError('limit/offset must be non-negative integers')

    def _select(self, c, where=None, *, columns=None, order_by=None, limit=None, offset=0):
        self._window(limit, offset)
        fields = list(self.columns) if columns is None else ([columns] if isinstance(columns, str) else list(columns))
        fields = [_unquote_identifier(str(f)) for f in fields]
        if any(f not in self._fields for f in fields):
            raise SchemaError('unknown projection column')
        condition = self._where_sql(where)
        ordering = _parse_order_by(order_by)
        if condition is None or any(self._types.get(f) in _OBJECT for f, desc in ordering):
            rows = [self._decode(r) for r in c.execute('SELECT '+','.join(self._fields.values())+' FROM data ORDER BY _pos')]
            predicate = _compile_where(where)
            rows = [row for row in rows if predicate(copy.deepcopy(row))]
            _sort_rows(rows, order_by)
            return [{f: r[f] for f in fields} for r in rows[offset:][:limit]]
        order = []
        for name, descending in ordering:
            field = self._fields[name]
            direction = 'DESC' if descending else 'ASC'
            order.extend([f'({field} IS NULL) {direction}', f'{field} {direction}'])
        order.append('_pos')
        predicate, values = condition
        projection = ','.join(self._fields[f] for f in fields) or '_pos'
        records = c.execute(f'SELECT {projection} FROM data WHERE {predicate} ORDER BY '+','.join(order)+
                            ' LIMIT ? OFFSET ?', (*values, -1 if limit is None else limit, offset))
        return [self._decode(r, fields) if fields else {} for r in records]

    def select(self, where=None, *, columns=None, order_by=None, limit=None, offset=0):
        return self._select(self._connection(), where, columns=columns, order_by=order_by, limit=limit, offset=offset)

    def find(self, where=None, *, order_by=None):
        rows = self.select(where, order_by=order_by, limit=1)
        return rows[0] if rows else None

    def all(self):
        return self.select()

    def count(self, where=None):
        if where is None:
            return len(self)
        condition = self._where_sql(where)
        if condition is None:
            return len(self.select(where))
        sql, values = condition
        return self._connection().execute('SELECT COUNT(*) FROM data WHERE '+sql, values).fetchone()[0]

    def _insert(self, c, rows):
        if rows:
            c.executemany('INSERT INTO data ('+','.join(self._fields.values())+') VALUES ('+
                          ','.join('?' for _ in self.columns)+')',
                          [tuple(self._encode(f, row[f]) for f in self.columns) for row in rows])
        return len(rows)

    def insert_many(self, rows):
        rows = [self._normalise_row(row, complete=True) for row in rows]
        self.definition.validate_rows(rows)
        return self._write(lambda c: self._insert(c, rows)) if rows else 0

    def insert_one(self, row):
        return self.insert_many([row])

    def append(self, row):
        self.insert_one(row)

    def extend(self, rows):
        self.insert_many(rows)

    def _change(self, c, kind, where, patch=None, limit=None, *, strict=False, upsert=None):
        condition = self._where_sql(where)
        if condition is None:
            raise ValueError('batch conditions require scalar equalities')
        sql, args = condition
        if strict:
            count = c.execute('SELECT COUNT(*) FROM data WHERE '+sql, args).fetchone()[0]
            if count > 1:
                raise ValueError('key is not unique')
            if not count:
                if upsert is None:
                    raise KeyError(where)
                return self._insert(c, [upsert])
        if limit is not None:
            sql = ' _pos IN (SELECT _pos FROM data WHERE '+sql+' ORDER BY _pos LIMIT ?)'
            args = [*args, limit]
        if kind == 'delete':
            return c.execute('DELETE FROM data WHERE '+sql, args).rowcount
        if not patch:
            return c.execute('SELECT COUNT(*) FROM data WHERE '+sql, args).fetchone()[0]
        assignments = ','.join(self._fields[f]+'=?' for f in patch)
        return c.execute('UPDATE data SET '+assignments+' WHERE '+sql,
                         [*(self._encode(f, v) for f,v in patch.items()), *args]).rowcount

    def update(self, values, where=None, *, limit=None):
        self._window(limit)
        patch = self._normalise_row(values)
        if self._where_sql(where) is not None:
            return self._write(lambda c: self._change(c, 'update', where, patch, limit))
        predicate = _compile_where(where)
        def apply(rows, columns):
            count = 0
            for row in rows:
                if (limit is None or count < limit) and predicate(copy.deepcopy(row)):
                    row.update(copy.deepcopy(patch)); count += 1
            return count
        return self._mutate(apply)

    def delete(self, where=None, *, limit=None):
        self._window(limit)
        if self._where_sql(where) is not None:
            return self._write(lambda c: self._change(c, 'delete', where, limit=limit))
        predicate = _compile_where(where)
        def remove(rows, columns):
            keep, count = [], 0
            for row in rows:
                if (limit is None or count < limit) and predicate(copy.deepcopy(row)):
                    count += 1
                else:
                    keep.append(row)
            rows[:] = keep
            return count
        return self._mutate(remove)

    def update_many_where(self, updates):
        prepared = [(copy.deepcopy(w), self._normalise_row(p)) for w,p in updates]
        return self._write(lambda c: sum(self._change(c, 'update', w, p) for w,p in prepared)) if prepared else 0

    def delete_many_where(self, wheres):
        wheres = copy.deepcopy(list(wheres))
        return self._write(lambda c: sum(self._change(c, 'delete', w) for w in wheres)) if wheres else 0

    def _mutate(self, callback, *, copy_rows=True, new_columns=()):
        """Apply a list-style mutation inside one SQLite transaction.

        The SQL parser is shared with the file-mapped table.  Its callback
        contract includes optional layout arguments that are meaningful only
        for the mmap backend; SQLite has a fixed schema, so those arguments
        are accepted and intentionally ignored here.
        """
        def change(c):
            rows = self._select(c)
            result = callback(rows, list(self.columns))
            rows = [self._normalise_row(r, complete=True) for r in rows]
            self.definition.validate_rows(rows)
            c.execute('DELETE FROM data')
            self._insert(c, rows)
            return copy.deepcopy(result)
        return self._write(change)

    def __len__(self):
        return self._connection().execute('SELECT row_count FROM metadata WHERE version=1').fetchone()[0]

    def __iter__(self):
        return iter(self.all())

    def __getitem__(self, index):
        if isinstance(index, int) and index >= 0:
            rows = self.select(offset=index, limit=1)
            if not rows:
                raise IndexError(index)
            return rows[0]
        return self.all()[index]

    def __setitem__(self, index, value):
        replacement = ([self._normalise_row(r, complete=True) for r in value] if isinstance(index, slice)
                       else self._normalise_row(value, complete=True))
        self._mutate(lambda rows, cols: rows.__setitem__(index, replacement))

    def __delitem__(self, index):
        self._mutate(lambda rows, cols: rows.__delitem__(index))

    def insert(self, index, value):
        row = self._normalise_row(value, complete=True)
        self._mutate(lambda rows, cols: rows.insert(index, row))

    def pop(self, index=-1):
        return self._mutate(lambda rows, cols: rows.pop(index))

    def sort(self, *, key=None, reverse=False):
        self._mutate(lambda rows, cols: _sort_rows(rows, [(key, 'DESC' if reverse else 'ASC')] if key else None))

    def clear(self):
        self.delete()

    def key_value(self, key_column=None):
        return SQLiteTableKeyValue(self, key_column)

    @property
    def kv(self):
        return self.key_value()

    def get_by_pk(self, key):
        return self.kv.require(key)

    def update_by_pk(self, key, values):
        return self.kv.update(key, values)

    def delete_by_pk(self, key):
        return self.kv.delete(key)

    def get_by_key(self, key, key_column=None):
        return self.key_value(key_column).require(key)

    def update_by_key(self, key, values, key_column=None):
        return self.key_value(key_column).update(key, values)

    def delete_by_key(self, key, key_column=None):
        return self.key_value(key_column).delete(key)

    def read_pipeline(self):
        return self.kv.read_pipeline()

    def transaction(self):
        return SQLiteTableWriteBatch(self)

    def close(self):
        # Call only after worker operations finish; close releases all thread connections.
        with self._connection_lock:
            for c in self._connections:
                c.close()
            self._connections.clear()
            self._closed = True

    execute = _SQLTable.execute
    _clauses = staticmethod(_SQLTable._clauses)
    _assert_table = _SQLTable._assert_table
    _execute_select = _SQLTable._execute_select
    _execute_update = _SQLTable._execute_update
    _execute_delete = _SQLTable._execute_delete
    _execute_insert = _SQLTable._execute_insert

    def _read_snapshot_columns(self):
        return list(self.columns)


class SQLiteTableKeyValue(MutableMapping):
    def __init__(self, table, key_column=None):
        self.table = table
        self.key_columns = (key_column,) if key_column else table.primary_keys
        for field in self.key_columns:
            table.definition.column(field)

    def _public(self, key):
        if len(self.key_columns) == 1:
            hash(key)
            return key
        if not isinstance(key, (tuple, list)) or len(key) != len(self.key_columns):
            raise KeyError(key)
        key = tuple(key)
        hash(key)
        return key

    def _where(self, key):
        key = self._public(key)
        return dict(zip(self.key_columns, (key,) if len(self.key_columns) == 1 else key))

    def require(self, key, *, fields=None):
        rows = self.table.select(self._where(key), columns=fields, limit=2)
        if not rows:
            raise KeyError(key)
        if len(rows) > 1:
            raise ValueError('key is not unique')
        return rows[0]

    def get(self, key, default=None, *, fields=None):
        try:
            return self.require(key, fields=fields)
        except KeyError:
            return default

    def contains(self, key):
        return self.get(key) is not None

    __contains__ = contains

    def get_many(self, keys):
        keys = list(dict.fromkeys(self._public(k) for k in keys))
        result = {}
        if not keys:
            return result
        wheres = [self._where(k) for k in keys]
        conditions = [self.table._where_sql(w) for w in wheres]
        with self.table._read_transaction() as c:
            if all(q is not None and all(v is not None for v in w.values())
                   for q,w in zip(conditions, wheres)):
                # Use native indexed IN lookups, rather than 500 Python/SQL
                # round trips. All chunks share this read snapshot.
                valid = [(k, tuple(w.values())) for k,w,q in zip(keys, wheres, conditions) if q[0] != '0']
                requested = {k: k for k in keys}
                width = len(self.key_columns)
                chunk_size = max(1, 900 // width)
                key_fields = ','.join(self.table._fields[k] for k in self.key_columns)
                projection = ','.join(self.table._fields.values())
                for start in range(0, len(valid), chunk_size):
                    chunk = valid[start:start+chunk_size]
                    placeholders = ','.join('('+','.join('?' for _ in range(width))+')' for _ in chunk)
                    csql = '('+key_fields+') IN (VALUES '+placeholders+')'
                    args = [v for key,values in chunk for v in values]
                    for record in c.execute('SELECT '+projection+' FROM data WHERE '+csql, args):
                        row = self.table._decode(record)
                        values = tuple(row[k] for k in self.key_columns)
                        key = requested[values[0] if width == 1 else values]
                        if key in result:
                            raise ValueError('key is not unique')
                        result[key] = row
                return {k: result[k] for k in keys if k in result}
            for key, where in zip(keys, wheres):
                rows = self.table._select(c, where, limit=2)
                if len(rows) > 1:
                    raise ValueError('key is not unique')
                if rows:
                    result[key] = rows[0]
        return result

    def _patch(self, key, values):
        patch = self.table._normalise_row(values)
        for name, value in self._where(key).items():
            if name in patch and patch[name] != value:
                raise ValueError('key mutation is not supported')
        return patch

    def update(self, key, values=None, *, upsert=False):
        if values is None:
            return self.update_many(key)
        patch = self._patch(key, values)
        where = self._where(key)
        row = self.table._normalise_row({**patch, **where}, complete=True) if upsert else None
        return self.table._write(lambda c: self.table._change(c, 'update', where, patch, strict=True, upsert=row))

    def set(self, key, row, *, upsert=False):
        replacement = self.table._normalise_row({**self._patch(key, row), **self._where(key)}, complete=True)
        return self.update(key, replacement, upsert=upsert)

    def update_many(self, updates):
        source = list(updates.items() if isinstance(updates, Mapping) else updates)
        keys = [self._public(k) for k,p in source]
        if len(set(keys)) != len(keys):
            raise ValueError('duplicate key in update_many')
        prepared = [(self._where(k), self._patch(k, p)) for k,p in source]
        return self.table._write(lambda c: sum(self.table._change(c, 'update', w, p, strict=True)
                                              for w,p in prepared)) if prepared else 0

    def delete_many(self, keys):
        keys = [self._public(k) for k in keys]
        if len(set(keys)) != len(keys):
            raise ValueError('duplicate key in delete_many')
        wheres = [self._where(k) for k in keys]
        return self.table._write(lambda c: sum(self.table._change(c, 'delete', w, strict=True)
                                              for w in wheres)) if wheres else 0

    def delete(self, key):
        return self.delete_many([key])

    def items(self):
        result, seen = [], set()
        for row in self.table.all():
            values = tuple(row[k] for k in self.key_columns)
            key = values[0] if len(values) == 1 else values
            if key in seen:
                raise ValueError('key is not unique')
            seen.add(key)
            result.append((key, row))
        return result

    def keys(self):
        return [key for key,row in self.items()]

    def values(self):
        return [row for key,row in self.items()]

    def __iter__(self):
        return iter(self.keys())

    def __len__(self):
        return len(self.table) if self.key_columns == self.table.primary_keys else len(self.keys())

    __getitem__ = require
    __delitem__ = delete

    def __setitem__(self, key, value):
        self.set(key, value, upsert=True)

    def read_pipeline(self):
        return SQLiteTableReadPipeline(self)


class SQLiteTableReadPipeline:
    def __init__(self, view):
        self.view, self._commands = view, []

    def get(self, key):
        self._commands.append(('get', key)); return self

    def contains(self, key):
        self._commands.append(('contains', key)); return self

    def get_many(self, keys):
        self._commands.append(('get_many', list(keys))); return self

    def clear(self):
        self._commands.clear(); return self

    def __len__(self):
        return len(self._commands)

    def execute(self):
        commands, self._commands = self._commands, []
        keys = [k for kind,v in commands for k in (v if kind == 'get_many' else [v])]
        rows = self.view.get_many(keys)
        result = []
        for kind, value in commands:
            if kind == 'get_many':
                keys = [self.view._public(k) for k in value]
                result.append({k: copy.deepcopy(rows[k]) for k in keys if k in rows})
            elif kind == 'contains':
                result.append(self.view._public(value) in rows)
            else:
                result.append(copy.deepcopy(rows.get(self.view._public(value))))
        return result


class SQLiteTableWriteBatch:
    def __init__(self, table):
        self.table, self._operations, self._closed = table, [], False

    def _queue(self, kind, value):
        if self._closed:
            raise RuntimeError('batch is closed')
        if self._operations and self._operations[0][0] != kind:
            raise ValueError('batch must contain one operation family')
        self._operations.append((kind, copy.deepcopy(value)))
        return self

    def update(self, values, where, *, limit=None):
        if limit is not None or self.table._where_sql(where) is None:
            raise ValueError('batch requires scalar equality without LIMIT')
        return self._queue('update', (where, self.table._normalise_row(values)))

    def update_by_pk(self, key, values):
        return self.update(values, self.table.kv._where(key))

    def update_by_key(self, key, values, key_column=None):
        return self.update(values, self.table.key_value(key_column)._where(key))

    def delete(self, where, *, limit=None):
        if limit is not None or self.table._where_sql(where) is None:
            raise ValueError('batch requires scalar equality without LIMIT')
        return self._queue('delete', where)

    def delete_by_pk(self, key):
        return self.delete(self.table.kv._where(key))

    def delete_by_key(self, key, key_column=None):
        return self.delete(self.table.key_value(key_column)._where(key))

    def insert(self, row):
        return self.insert_many([row])

    def insert_many(self, rows):
        return self._queue('insert', [self.table._normalise_row(r, complete=True) for r in rows])

    def commit(self):
        if self._closed:
            raise RuntimeError('batch is closed')
        self._closed = True
        if not self._operations:
            return 0
        kind = self._operations[0][0]
        values = [v for k,v in self._operations]
        if kind == 'insert':
            return self.table.insert_many([r for batch in values for r in batch])
        if kind == 'update':
            return self.table.update_many_where(values)
        return self.table.delete_many_where(values)

    def rollback(self):
        self._operations.clear(); self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if not self._closed:
            self.commit() if kind is None else self.rollback()


def _writer_error(exc, *, unknown=False):
    return {'error': type(exc).__name__, 'message': str(exc), 'unknown': unknown}


def _writer_result(response):
    if 'error' not in response:
        return response['result']
    error = {'SchemaError': SchemaError, 'KeyError': KeyError,
             'ValueError': ValueError, 'TypeError': TypeError,
             'SQLiteBusyError': SQLiteBusyError}.get(response['error'], SQLiteWriteProcessError)
    if response.get('unknown'):
        error = SQLiteWriteProcessError
    raise error(response['message'])


def _prepare_writer_operation(table, operation, payload):
    """Validate before the write lock; return a callback used once, never retried."""
    if operation == 'insert':
        rows = [table._normalise_row(row, complete=True) for row in payload]
        table.definition.validate_rows(rows)
        return lambda c: table._insert(c, rows)
    if operation in ('update', 'delete'):
        if operation == 'update':
            patch, where, limit = payload
            patch = table._normalise_row(patch)
        else:
            where, limit = payload
            patch = None
        table._window(limit)
        if table._where_sql(where) is None:
            raise ValueError('writer conditions require scalar equalities')
        return lambda c: table._change(c, operation, where, patch, limit)
    if operation in ('update_keys', 'delete_keys'):
        key_column, items = payload
        view = table.key_value(key_column)
        keys = [view._public(item[0] if operation == 'update_keys' else item) for item in items]
        if len(set(keys)) != len(keys):
            raise ValueError('duplicate key in writer batch')
        if operation == 'update_keys':
            prepared = [(view._where(key), view._patch(key, patch)) for key, patch in items]
            return lambda c: sum(table._change(c, 'update', where, patch, strict=True)
                                 for where, patch in prepared)
        wheres = [view._where(key) for key in items]
        return lambda c: sum(table._change(c, 'delete', where, strict=True) for where in wheres)
    raise ValueError(f'unknown writer operation: {operation}')


class _SQLiteWriterService:
    """One process, one SQLite write thread; RPC threads only enqueue/wait.

    BaseManager supplies authenticated local IPC and a separate connection per
    calling thread. Unlike a Manager.Queue transport this needs no extra broker
    process, and no reply queue proxy is serialized for each operation.
    """
    def __init__(self, config, batch_size, flush_interval, queue_size, request_timeout):
        self._config = config
        self._batch_size, self._interval = batch_size, flush_interval
        self._timeout = request_timeout
        self._queue = queue.Queue(queue_size)
        self._gate = threading.Lock()
        self._accepting = True
        self._ready, self._stopped = threading.Event(), threading.Event()
        self._failure = None
        self._stats = dict(commits=0, requests=0, succeeded=0, failed=0,
                           max_group=0, active_requests=0)
        self._thread = threading.Thread(target=self._run, name='sqlite-group-commit', daemon=True)
        self._thread.start()

    def status(self):
        if not self._ready.wait(self._timeout):
            return _writer_error(SQLiteWriteProcessError('writer startup timed out'))
        if self._failure:
            return self._failure
        return {'result': {'pid': os.getpid(), 'queued_requests': self._queue.qsize(), **self._stats}}

    def _enqueue(self, item):
        # Admission and close share this gate: stop cannot overtake an admitted
        # request. Backpressure is bounded rather than an unbounded RAM log.
        with self._gate:
            if not self._accepting or self._failure:
                return _writer_error(SQLiteWriteProcessError('writer is closed or failed; request not accepted'))
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                return _writer_error(SQLiteBusyError('writer queue is full; request not accepted'))
        if not item['done'].wait(self._timeout):
            return _writer_error(SQLiteWriteProcessError(
                f"request {item['token']} outcome unknown after timeout; do not retry blindly"), unknown=True)
        return item['response']

    def execute(self, table, operation, payload, token):
        return self._enqueue(dict(table=table, operation=operation, payload=payload,
                                  token=token, done=threading.Event()))

    def flush(self):
        # A barrier covers requests admitted before it, not calls that another
        # producer has not yet submitted. Producers must finish before close.
        return self._enqueue(dict(table=None, operation='flush', token=uuid.uuid4().hex,
                                  done=threading.Event()))

    def stop(self):
        with self._gate:
            self._accepting = False
        if not self._stopped.wait(self._timeout):
            return _writer_error(SQLiteWriteProcessError(
                'writer shutdown timed out; pending request outcomes unknown'), unknown=True)
        return self._failure or {'result': True}

    @staticmethod
    def _finish(item, response):
        item['response'] = response
        item['done'].set()

    def _commit(self, db, group):
        self._stats['active_requests'] = len(group)
        prepared, responses = [], {}
        try:
            table = db[group[0]['table']]
            for i, request in enumerate(group):
                try:
                    prepared.append((i, _prepare_writer_operation(
                        table, request['operation'], request['payload'])))
                except (ValueError, TypeError, KeyError) as exc:
                    responses[i] = _writer_error(exc)

            def apply(c):
                for i, callback in prepared:
                    c.execute('SAVEPOINT request')
                    try:
                        result = callback(c)
                    except (sqlite3.IntegrityError, ValueError, TypeError, KeyError) as exc:
                        c.execute('ROLLBACK TO request')
                        responses[i] = _writer_error(SchemaError(str(exc))
                            if isinstance(exc, sqlite3.IntegrityError) else exc)
                    else:
                        responses[i] = {'result': result}
                    c.execute('RELEASE request')

            if prepared:
                table._write(apply)
                self._stats['commits'] += 1
        except Exception as exc:
            # Lock acquisition failure is known not to have started. I/O and
            # ambiguous COMMIT failures are never automatically replayed.
            response = _writer_error(exc, unknown=not isinstance(exc, (SQLiteBusyError, SchemaError)))
            responses = {i: response for i in range(len(group))}
        self._stats['requests'] += len(group)
        self._stats['max_group'] = max(self._stats['max_group'], len(group))
        for i, item in enumerate(group):
            response = responses[i]
            self._stats['failed' if 'error' in response else 'succeeded'] += 1
            # No acknowledgement, including a success from an earlier savepoint,
            # escapes until the outer FULL-synchronous COMMIT has returned.
            self._finish(item, response)
        self._stats['active_requests'] = 0

    def _run(self):
        pending, current = None, []
        try:
            with SQLiteDatabase(**self._config) as db:
                self._ready.set()
                while True:
                    if pending is not None:
                        request, pending = pending, None
                    else:
                        try:
                            request = self._queue.get(timeout=0.01)
                        except queue.Empty:
                            with self._gate:
                                if not self._accepting and self._queue.empty():
                                    break
                            continue
                    current = [request]
                    if request['operation'] == 'flush':
                        self._finish(request, {'result': True})
                        current = []
                        continue
                    # perf_counter is monotonic AND high-resolution on Python
                    # 3.11/Windows; monotonic() there uses 15.625 ms GetTickCount64.
                    deadline = time.perf_counter() + self._interval
                    while len(current) < self._batch_size:
                        try:
                            # Always drain immediately available work, including
                            # when flush_interval=0 (no intentional wait).
                            candidate = self._queue.get_nowait()
                        except queue.Empty:
                            remaining = deadline - time.perf_counter()
                            if remaining <= 0:
                                break
                            # Windows Condition/Queue timed waits can round a
                            # 1 ms budget up to a ~15.6 ms timer tick. Python
                            # 3.11's sleep uses a high-resolution waitable timer.
                            # Yield briefly, then drain; no spin or global timer
                            # resolution change, and no added durability delay
                            # after this collection window.
                            time.sleep(min(remaining, 0.00025))
                            continue
                        if candidate['table'] == request['table'] and candidate['operation'] != 'flush':
                            current.append(candidate)
                        else:
                            pending = candidate
                            break
                    self._commit(db, current)
                    current = []
        except BaseException as exc:
            self._failure = _writer_error(SQLiteWriteProcessError(
                f'writer failed; pending outcomes unknown: {exc}'), unknown=True)
        finally:
            with self._gate:
                self._accepting = False
            for item in current + ([pending] if pending else []):
                self._finish(item, self._failure or _writer_error(SQLiteWriteProcessError('writer stopped')))
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._finish(item, self._failure or _writer_error(SQLiteWriteProcessError('writer stopped')))
            self._ready.set()
            self._stopped.set()


_writer_service = None


def _initialize_writer_service(*args):
    global _writer_service
    _writer_service = _SQLiteWriterService(*args)


def _get_writer_service():
    return _writer_service


class _SQLiteWriterManager(BaseManager):
    pass


_SQLiteWriterManager.register('writer', callable=_get_writer_service,
                              exposed=('execute', 'flush', 'status', 'stop'))


class SQLiteWriteClient:
    """Share this endpoint with spawn workers; each thread connects locally.

    All methods are synchronous durable acknowledgements. ``insert_many`` and
    key batches are atomic per request; independent producers' requests share
    a commit but use savepoints to isolate validation/constraint failures.
    """
    def __init__(self, address, authkey):
        self._address, self._authkey = address, bytes(authkey)
        self._local = threading.local()

    def __getstate__(self):
        return self._address, self._authkey

    def __setstate__(self, state):
        self.__init__(*state)

    def _proxy(self):
        if getattr(self._local, 'pid', None) != os.getpid():
            manager = _SQLiteWriterManager(address=self._address, authkey=self._authkey)
            manager.connect()
            self._local.proxy = manager.writer()
            self._local.manager, self._local.pid = manager, os.getpid()
        return self._local.proxy

    def _call(self, method, *args):
        try:
            response = getattr(self._proxy(), method)(*args)
        except (EOFError, OSError) as exc:
            # No replay: the commit may have succeeded just before IPC failed.
            SQLiteWriteClient.close(self)
            raise SQLiteWriteProcessError(
                'writer connection lost; outcome unknown, do not retry blindly') from exc
        return _writer_result(response)

    def _request(self, table, operation, payload):
        table = _validate_identifier(table, label='table name')
        return self._call('execute', table, operation, payload, uuid.uuid4().hex)

    def connect(self):
        self._call('status')
        return self

    def insert_many(self, table, rows):
        return self._request(table, 'insert', list(rows))

    def append(self, table, row):
        return self.insert_many(table, [row])

    def update(self, table, values, where=None, *, limit=None):
        return self._request(table, 'update', (values, where, limit))

    def update_many(self, table, updates, *, key_column=None):
        items = list(updates.items() if isinstance(updates, Mapping) else updates)
        return self._request(table, 'update_keys', (key_column, items))

    def update_by_pk(self, table, key, values):
        return self.update_many(table, [(key, values)])

    def update_by_key(self, table, key, values, key_column=None):
        return self.update_many(table, [(key, values)], key_column=key_column)

    def delete(self, table, where=None, *, limit=None):
        return self._request(table, 'delete', (where, limit))

    def delete_many(self, table, keys, *, key_column=None):
        return self._request(table, 'delete_keys', (key_column, list(keys)))

    def delete_by_pk(self, table, key):
        return self.delete_many(table, [key])

    def delete_by_key(self, table, key, key_column=None):
        return self.delete_many(table, [key], key_column=key_column)

    def flush(self):
        return self._call('flush')

    def stats(self):
        return self._call('status')

    def close(self):
        # Release this calling thread's IPC proxy; other threads own theirs.
        self._local.__dict__.clear()


class SQLiteWriteProcess(SQLiteWriteClient):
    """Optional single writer process with bounded group commit.

    Create under ``if __name__ == '__main__'``. Pass ``writer.client()`` to
    producers, and finish/join all producers before closing this owner. Reads
    stay on SQLiteDatabase connections. The IPC transport is not a durable log;
    only a successful response certifies the FULL-synchronous SQLite commit.
    """
    def __init__(self, directory=None, *, schema_directory=None, busy_timeout=5.0,
                 transaction_retries=3, mmap_size=256 * 1024 * 1024,
                 batch_size=256, flush_interval=0.001, queue_size=1024,
                 request_timeout=30.0, start_method='spawn'):
        if (not isinstance(batch_size, int) or batch_size < 1 or
                not isinstance(queue_size, int) or queue_size < 1 or
                not math.isfinite(flush_interval) or flush_interval < 0 or
                not math.isfinite(request_timeout) or request_timeout <= 0 or
                not math.isfinite(busy_timeout) or busy_timeout < 0 or
                transaction_retries < 1 or mmap_size < 0):
            raise ValueError('invalid SQLite writer configuration')
        directory = Path(directory or Path.cwd() / 'dat').expanduser().resolve()
        schema_directory = (Path(schema_directory).expanduser().resolve()
                            if schema_directory else directory.parent / 'def/table')
        self._config = dict(directory=str(directory), schema_directory=str(schema_directory),
                            busy_timeout=busy_timeout, transaction_retries=transaction_retries,
                            mmap_size=mmap_size)
        self._options = (batch_size, flush_interval, queue_size, request_timeout)
        # Windows named pipes / Unix domain sockets: local IPC only, random
        # authentication key. No TCP listener and no external Redis/service.
        self._owner = _SQLiteWriterManager(authkey=os.urandom(32), ctx=mp.get_context(start_method))
        self._owner_pid = os.getpid()
        self._started = self._closed = False
        self._lifecycle = threading.RLock()

    def __getstate__(self):
        raise TypeError('pass writer.client() to workers, not the process owner')

    def _check_owner(self):
        if os.getpid() != self._owner_pid:
            raise SQLiteWriteProcessError('writer owner belongs to another process; pass client()')

    def start(self):
        self._check_owner()
        with self._lifecycle:
            if self._closed:
                raise SQLiteWriteProcessError('writer is closed')
            if not self._started:
                self._owner.start(initializer=_initialize_writer_service,
                                  initargs=(self._config, *self._options))
                super().__init__(self._owner.address, self._owner._authkey)
                try:
                    super()._call('status')
                except BaseException:
                    self._owner.shutdown()
                    self._closed = True
                    raise
                self._started = True
        return self

    def _call(self, method, *args):
        self.start()
        return super()._call(method, *args)

    @property
    def pid(self):
        return self._owner._process.pid if self._started else None

    def client(self):
        self.start()
        return SQLiteWriteClient(self._address, self._authkey)

    def close(self):
        self._check_owner()
        with self._lifecycle:
            if self._closed:
                return
            try:
                if self._started:
                    super()._call('stop')
            finally:
                self._closed = True
                if self._started:
                    self._owner.shutdown()
                    super().close()

    def __enter__(self):
        return self.start()

    def __exit__(self, kind, exc, tb):
        self.close()

class SQLiteDatabase:
    def __init__(self, directory=None, *, schema_directory=None, busy_timeout=5.0,
                 transaction_retries=3, mmap_size=256 * 1024 * 1024):
        if busy_timeout < 0 or transaction_retries < 1 or mmap_size < 0:
            raise ValueError('invalid SQLite timeout/retry/mmap configuration')
        self.directory = Path(directory or Path.cwd() / 'dat').expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.schema_directory = Path(schema_directory) if schema_directory else self.directory.parent / 'def/table'
        self.schema_registry = SchemaRegistry(self.schema_directory)
        self.busy_timeout, self.transaction_retries = float(busy_timeout), int(transaction_retries)
        self.mmap_size = int(mmap_size)
        self._pid, self._closed = os.getpid(), False
        self._tables, self._lock = {}, threading.RLock()

    def _check_open(self):
        if os.getpid() != self._pid:
            raise SQLiteStorageError('open a new SQLiteDatabase in each process')
        if self._closed:
            raise SQLiteStorageError('database is closed')

    def table(self, name):
        self._check_open()
        name = _validate_identifier(name, label='table name')
        with self._lock:
            if name not in self._tables:
                self._tables[name] = SQLiteTable(self, self.schema_registry.load(name))
            return self._tables[name]
    __getitem__ = table
    execute = FileMappedDatabase.execute

    def write_process(self, **options):
        """Create a dedicated writer owner with this database's configuration."""
        self._check_open()
        config = dict(directory=self.directory, schema_directory=self.schema_directory,
                      busy_timeout=self.busy_timeout, transaction_retries=self.transaction_retries,
                      mmap_size=self.mmap_size)
        config.update(options)
        return SQLiteWriteProcess(**config)

    def refresh_schema(self):
        self._check_open()
        with self._lock:
            for table in self._tables.values():
                table.close()
            self._tables.clear()
            self.schema_registry.clear()

    def close(self):
        if os.getpid() != self._pid:
            raise SQLiteStorageError('do not close inherited SQLite connections')
        with self._lock:
            for table in self._tables.values():
                table.close()
            self._tables.clear()
            self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, kind, exc, tb):
        self.close()
