"""Schema-defined Redis tables, alongside the file-mapped backend.

Rows use binary MessagePack fields in Redis hashes; equality indexes and row
order use sorted sets. Lua validates a complete write plan before applying it.
Point/equality operations run on the server; arbitrary Python predicates and
list reordering use a generation-checked snapshot/compare-and-swap fallback.
Only metadata definitions use JSON. No Python row cache is kept.

Requires redis-py >= 5, msgpack >= 1 and standalone Redis >= 7. Default writes
require AOF always/noeviction configuration. ``durability='server'`` explicitly
accepts the server's configured durability instead. A network failure can
leave a commit's outcome unknown; writes are never automatically replayed.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import threading
from collections.abc import Mapping, MutableMapping, MutableSequence
from pathlib import Path

try:
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
except ImportError:  # optional backend
    redis = None
try:
    import msgpack
except ImportError:  # optional backend
    msgpack = None

from .file_mapped_db import (
    FileMappedDatabase, Table as _SQLTable,
    _compile_where, _parse_equality_mapping, _sort_rows, _unquote_identifier,
    _validate_identifier,
)
from .table_schema import SchemaError, SchemaRegistry

__all__ = ['RedisDatabase', 'RedisTable', 'RedisTableKeyValue',
           'RedisTableReadPipeline', 'RedisTableWriteBatch', 'RedisStorageError',
           'RedisDurabilityError']


class RedisStorageError(RuntimeError):
    """Backend/configuration failure; a transport error may have committed."""


class RedisDurabilityError(RedisStorageError):
    """Write committed but requested replication acknowledgement was not met."""


def _pack(value):
    return msgpack.packb(value, use_bin_type=False)


def _unpack(value):
    return msgpack.unpackb(value, raw=False, strict_map_key=False)


def _text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else str(value)


def _canonical(value):
    # Match Python equality for integral floats, booleans and negative zero.
    # Never pass numeric row values through Lua's double precision numbers.
    if isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('non-finite numbers are not supported')
        if int(value) == value:
            return ['number', str(int(value))]
        return ['number', repr(value)]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError('object keys must be strings')
        return ['object', [[key, _canonical(value[key])] for key in sorted(value)]]
    if isinstance(value, list):
        return ['array', [_canonical(item) for item in value]]
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f'unsupported value type: {type(value).__name__}')


def _token(value):
    return base64.urlsafe_b64encode(_pack(_canonical(value))).decode('ascii').rstrip('=')


# All user errors, capacity/uniqueness failures and wrong Redis key types are
# checked before the first write. Lua itself does not roll back runtime errors.
# Namespace keys must be exclusively owned by this module, with no TTL/eviction.
_SCRIPT = r'''
local base = KEYS[1]
local meta = base .. ':meta'
local order = base .. ':order'
local rp = base .. ':row:'
local ip = base .. ':idx:'
local p = cmsgpack.unpack(ARGV[2])
local function typ(k, expected)
    local t = redis.call('TYPE', k).ok
    if t ~= 'none' and t ~= expected then error('RTDB_TYPE:' .. k) end
end
typ(meta, 'hash')
if p.op == 'init' then
    local old = redis.call('HGET', meta, 'schema')
    if old and old ~= ARGV[1] then return {0, 'schema'} end
    if not old then
        redis.call('HSET', meta, 'schema', ARGV[1], 'generation', '0',
                   'next', '0', 'definition', p.definition)
    end
    return {1}
end
if redis.call('HGET', meta, 'schema') ~= ARGV[1] then return {0, 'schema'} end
typ(order, 'zset')
local generation = redis.call('HGET', meta, 'generation')
local function ik(f, v) return ip .. f .. ':' .. v end
local loaded = {}
local function load(id)
    if loaded[id] ~= nil then return loaded[id] end
    typ(rp .. id, 'hash')
    local a = redis.call('HGETALL', rp .. id)
    if #a == 0 then loaded[id]=false; return false end
    local r = {}
    for i=1,#a,2 do r[a[i]]=a[i+1] end
    loaded[id]=r
    return r
end
local function flat(r)
    local a = {}
    for k,v in pairs(r) do
        if string.sub(k,1,1) ~= '!' then a[#a+1]=k; a[#a+1]=v end
    end
    return cmsgpack.pack(a)
end
local shadow, originals, touched, scores = {}, {}, {}, {}
local function current(id)
    if shadow[id] ~= nil then return shadow[id] end
    return load(id)
end
local function matches(r,q)
    if not r then return false end
    for _, cond in ipairs(q.eq or {}) do
        if r['!' .. cond[1]] ~= cond[2] then return false end
    end
    return true
end
local function candidates(q)
    if q.limit == 0 then return {} end
    local offset = q.offset or 0
    local ids
    if q.id then ids = {q.id}
    else
        local best, size = order, nil
        local index_keys = {}
        for _,cond in ipairs(q.eq or {}) do
            local k = ik(cond[1],cond[2]); typ(k, 'zset')
            index_keys[#index_keys+1] = k
            local n = redis.call('ZCARD',k)
            if not size or n < size then size=n; best=k end
        end
        -- Bounded multi-condition queries first try a small prefix, avoiding
        -- a full intersection for common first-row hits. If the prefix cannot
        -- fill the window, Redis intersects in C instead of loading every
        -- nonmatching row into Lua. Identical index scores preserve row order.
        -- Staged writes need shadow-aware filtering; unbounded reads retain
        -- the scan path to avoid intersection overhead on dense full results.
        if #index_keys > 1 and #touched == 0 and size > 128 and q.limit and q.limit > 0 then
            if (q.offset or 0) + q.limit <= 128 then
                local result, skipped = {}, 0
                for _,id in ipairs(redis.call('ZRANGE',best,0,127)) do
                    if matches(current(id),q) then
                        if skipped < (q.offset or 0) then skipped=skipped+1
                        else
                            result[#result+1]=id
                            if #result >= q.limit then return result end
                        end
                    end
                end
            end
            ids = redis.call('ZINTER', #index_keys, unpack(index_keys))
        else
            -- Single equality and plain range reads already have physical order.
            -- Multiple equalities must filter before applying OFFSET/LIMIT.
            -- Staged changes also invalidate early limits.
            local start, stop = 0, -1
            if #touched == 0 and #index_keys <= 1 then
                -- Stable indexes already represent the entire predicate.
                -- Seek to the requested rank without decoding skipped rows.
                start = offset
                offset = 0
                if q.limit and q.limit >= 0 then stop = start + q.limit - 1 end
            end
            ids = redis.call('ZRANGE', best, start, stop)
            if #touched > 0 then
                local seen = {}; for _,id in ipairs(ids) do seen[id]=true end
                for _,id in ipairs(touched) do if not seen[id] then ids[#ids+1]=id end end
                table.sort(ids,function(a,b)
                    return (scores[a] or tonumber(redis.call('ZSCORE', order,a)) or math.huge)
                         < (scores[b] or tonumber(redis.call('ZSCORE', order,b)) or math.huge)
                end)
            end
        end
    end
    local result, skipped = {}, 0
    for _,id in ipairs(ids) do
        if matches(current(id),q) then
            if skipped < offset then skipped=skipped+1
            else
                result[#result+1]=id
                if q.limit and q.limit >= 0 and #result >= q.limit then break end
            end
        end
    end
    return result
end
if p.op == 'read' then
    local groups = {}
    for _,q in ipairs(p.queries) do
        local rows = {}
        for _,id in ipairs(candidates(q)) do rows[#rows+1]={id,flat(current(id))} end
        groups[#groups+1]=rows
    end
    return {1,generation,groups}
end
if p.op == 'commit' and p.generation ~= generation then return {0,'conflict'} end
local nextscore=tonumber(redis.call('HGET',meta,'next')) or 0
local newcount=redis.call('ZCARD',order)
local function stage(id,r)
    if originals[id] == nil then
        originals[id]=load(id)
        touched[#touched+1]=id
        scores[id]=tonumber(redis.call('ZSCORE',order,id))
    end
    shadow[id]=r
end
local function decode(a)
    local r={}; for i=1,#a,2 do r[a[i]]=a[i+1] end; return r
end
local affected=0
if p.op == 'insert' then
    local seen={}
    for _,item in ipairs(p.rows) do
        if seen[item[1]] or current(item[1]) then return {0,'duplicate'} end
        seen[item[1]]=true
        stage(item[1],decode(item[2])); affected=affected+1
    end
elseif p.op == 'patch' or p.op == 'delete' then
    for _,cmd in ipairs(p.commands) do
        local ids=candidates(cmd.query)
        if cmd.strict and #ids == 0 and not cmd.upsert then return {0,'missing'} end
        if cmd.strict and #ids > 1 then return {0,'nonunique'} end
        if #ids == 0 and cmd.upsert then
            local id=cmd.upsert[1]
            if current(id) then return {0,'duplicate'} end
            stage(id,decode(cmd.upsert[2])); affected=affected+1
        end
        for _,id in ipairs(ids) do
            if p.op == 'delete' then stage(id,false)
            else
                local r=current(id)
                local changed={}; for k,v in pairs(r) do changed[k]=v end
                for i=1,#cmd.fields,2 do changed[cmd.fields[i]]=cmd.fields[i+1] end
                for _,f in ipairs(p.primary) do
                    if changed['!'..f] ~= r['!'..f] then return {0,'primary'} end
                end
                stage(id,changed)
            end
            affected=affected+1
        end
    end
elseif p.op == 'commit' then
    local seen={}
    for n,item in ipairs(p.rows) do
        if seen[item[1]] then return {0,'duplicate'} end
        seen[item[1]]=true; stage(item[1],decode(item[2])); scores[item[1]]=n
    end
    for _,id in ipairs(redis.call('ZRANGE',order,0,-1)) do
        if not seen[id] then stage(id,false) end
    end
    nextscore=#p.rows; affected=#touched
else return {0,'operation'} end
-- Validate the whole plan, including every index type, before publishing.
for _,id in ipairs(touched) do
    local old,r=originals[id],shadow[id]
    if not old and r then newcount=newcount+1 end
    if old and not r then newcount=newcount-1 end
    for _,row in ipairs({old or {},r or {}}) do
        for f,v in pairs(row) do
            if string.sub(f,1,1)=='!' then typ(ik(string.sub(f,2),v),'zset') end
        end
    end
end
for _,id in ipairs(touched) do
    local old,r=originals[id],shadow[id]
    local score=scores[id]
    if r and not score then nextscore=nextscore+1; score=nextscore end
    if old then
        for f,v in pairs(old) do
            if string.sub(f,1,1)=='!' and (not r or r[f]~=v) then
                redis.call('ZREM',ik(string.sub(f,2),v),id)
            elseif r and r[f]==nil then redis.call('HDEL',rp..id,f) end
        end
    end
    if r then
        for f,v in pairs(r) do
            if not old or old[f]~=v then redis.call('HSET',rp..id,f,v) end
            if string.sub(f,1,1)=='!' and (not old or old[f]~=v or p.op=='commit') then
                redis.call('ZADD',ik(string.sub(f,2),v),score,id)
            end
        end
        if not old or p.op=='commit' then redis.call('ZADD',order,score,id) end
    else redis.call('DEL',rp..id); redis.call('ZREM',order,id) end
end
if #touched > 0 then
    redis.call('HSET',meta,'next',nextscore)
    redis.call('HINCRBY',meta,'generation',1)
end
return {1,affected}
'''


class RedisTable(MutableSequence):
    def __init__(self, database, definition):
        self.database = database
        self.redis = database.redis
        self.definition = self.schema = definition
        self.name = definition.eng_name
        self.primary_keys = definition.primary_keys
        self.columns = definition.column_names
        self.base = database.namespace + ':' + self.name
        self._meta_key = self.base + ':meta'
        self._order_key = self.base + ':order'
        self._fingerprint = hashlib.sha256(json.dumps(definition.to_mapping(), sort_keys=True,
                                                     ensure_ascii=False).encode()).hexdigest()
        self._script = self.redis.register_script(_SCRIPT)
        self._call({'op': 'init', 'definition': _pack(definition.to_mapping())}, write=True)

    def _call(self, payload, *, write=False):
        payload.update(primary=list(self.primary_keys))
        def run(client):
            result = self._script(keys=[self.base], args=[self._fingerprint, _pack(payload)], client=client)
            if not result[0]:
                code = _text(result[1])
                if code == 'conflict':
                    return None
                if code == 'missing':
                    raise KeyError('key not found')
                if code in ('duplicate', 'capacity', 'schema', 'primary'):
                    raise SchemaError(f'Redis {self.name}: {code}')
                if code == 'nonunique':
                    raise ValueError('key is not unique')
                raise RedisStorageError(f'Redis operation rejected: {code}')
            if write and self.database.wait_replicas:
                # WAIT refers to writes on this exact connection. It is not a
                # disk-fsync acknowledgement, and failure cannot undo a write.
                count = client.wait(self.database.wait_replicas, self.database.wait_timeout_ms)
                if count < self.database.wait_replicas:
                    raise RedisDurabilityError(f'write committed; only {count} replicas acknowledged')
            return result
        if write and self.database.wait_replicas:
            with self.redis.client() as connection:
                return run(connection)
        return run(self.redis)

    def _normalise_row(self, row, *, complete=False):
        if not isinstance(row, Mapping):
            raise TypeError('rows/patches must be mappings')
        value = copy.deepcopy(dict(row))
        unknown = set(value) - set(self.columns)
        if unknown:
            raise SchemaError(f'unknown columns: {unknown}')
        for field, item in value.items():
            _canonical(item)
            if item is not None:
                self.definition._validate_value(item, self.definition.column(field), 1)
        if complete:
            self.definition.validate_rows([value])
            value = {field: value.get(field) for field in self.columns}
        return value

    def _fields(self, row):
        result = []
        for field, value in row.items():
            result.extend([field, _pack(value), '!' + field, _token(value)])
        return result

    def _key_values(self, key, columns=None):
        columns = columns or self.primary_keys
        if len(columns) == 1:
            values = (key,)
        elif isinstance(key, (list, tuple)) and len(key) == len(columns):
            values = tuple(key)
        else:
            raise KeyError(key)
        for value in values:
            _canonical(value)
            try:
                hash(value)
            except TypeError as exc:
                raise KeyError(key) from exc
        return values

    def _id(self, values):
        return base64.urlsafe_b64encode(_pack([_token(v) for v in values])).decode('ascii')

    def _spec(self, row):
        return [self._id(tuple(row[k] for k in self.primary_keys)), self._fields(row)]

    def _query(self, where=None, *, limit=None, offset=0):
        if limit is not None and limit < 0 or offset < 0:
            raise ValueError('limit/offset must be non-negative')
        eq = where if isinstance(where, Mapping) else _parse_equality_mapping(where)
        if where is not None and eq is None:
            return None
        eq = eq or {}
        if any(field not in self.columns for field in eq):
            return None
        q = {'eq': [[field, _token(value)] for field, value in eq.items()],
             'limit': -1 if limit is None else limit, 'offset': offset}
        if all(k in eq for k in self.primary_keys):
            q['id'] = self._id(tuple(eq[k] for k in self.primary_keys))
        return q

    def _decode(self, pair):
        fields = msgpack.unpackb(pair[1], raw=True)
        return {_text(fields[i]): _unpack(fields[i+1]) for i in range(0, len(fields), 2)
                if not fields[i].startswith(b'!')}

    def _read(self, queries):
        result = self._call({'op': 'read', 'queries': queries})
        return _text(result[1]), [[self._decode(pair) for pair in group] for group in result[2]]

    def select(self, where=None, *, columns=None, order_by=None, limit=None, offset=0):
        q = self._query(where, limit=limit if not order_by else None, offset=offset if not order_by else 0)
        if q is None:
            rows = self._read([self._query()])[1][0]
            predicate = _compile_where(where)
            rows = [row for row in rows if predicate(copy.deepcopy(row))]
        else:
            rows = self._read([q])[1][0]
        _sort_rows(rows, order_by)
        if q is None or order_by:
            rows = rows[offset:][:limit]
        if columns is not None:
            columns = [columns] if isinstance(columns, str) else columns
            fields = [_unquote_identifier(str(f)) for f in columns]
            rows = [{f: row.get(f) for f in fields} for row in rows]
        return rows

    def find(self, where=None, *, order_by=None):
        rows = self.select(where, order_by=order_by, limit=1)
        return rows[0] if rows else None

    def all(self):
        return self.select()

    def count(self, where=None):
        return len(self) if where is None else len(self.select(where))

    def insert_many(self, rows):
        rows = [self._normalise_row(row, complete=True) for row in rows]
        if not rows:
            return 0
        self.definition.validate_rows(rows)
        return int(self._call({'op': 'insert', 'rows': [self._spec(r) for r in rows]}, write=True)[1])

    def insert_one(self, row):
        return self.insert_many([row])

    def append(self, row):
        self.insert_many([row])

    def extend(self, rows):
        self.insert_many(rows)

    def _commands(self, kind, commands):
        if not commands:
            return 0
        return int(self._call({'op': kind, 'commands': commands}, write=True)[1])

    def update(self, values, where=None, *, limit=None):
        patch = self._normalise_row(values)
        q = self._query(where, limit=limit)
        if q is not None:
            return self._commands('patch', [{'query': q, 'fields': self._fields(patch)}])
        predicate = _compile_where(where)
        def apply(rows, columns):
            count = 0
            for row in rows:
                if (limit is None or count < limit) and predicate(copy.deepcopy(row)):
                    row.update(copy.deepcopy(patch)); count += 1
            return count
        return self._mutate(apply)

    def update_many_where(self, updates):
        commands = []
        for where, patch in updates:
            q = self._query(where)
            if q is None:
                raise ValueError('batch conditions require equality mappings')
            commands.append({'query': q, 'fields': self._fields(self._normalise_row(patch))})
        return self._commands('patch', commands)

    def delete(self, where=None, *, limit=None):
        q = self._query(where, limit=limit)
        if q is not None:
            return self._commands('delete', [{'query': q}])
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

    def delete_many_where(self, wheres):
        commands = []
        for where in wheres:
            q = self._query(where)
            if q is None:
                raise ValueError('batch conditions require equality mappings')
            commands.append({'query': q})
        return self._commands('delete', commands)

    def _mutate(self, callback):
        # Generic/list operations may be O(N); the callback is reevaluated on
        # contention. No data is removed before the replacement is validated.
        for _ in range(self.database.transaction_retries):
            generation, groups = self._read([self._query()])
            rows = groups[0]
            result = callback(rows, list(self.columns))
            rows = [self._normalise_row(row, complete=True) for row in rows]
            self.definition.validate_rows(rows)
            reply = self._call({'op': 'commit', 'generation': generation,
                                'rows': [self._spec(r) for r in rows]}, write=True)
            if reply is not None:
                return copy.deepcopy(result)
        raise RedisStorageError('table changed during snapshot commit; retry limit reached')

    def __len__(self):
        return int(self.redis.zcard(self._order_key))

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
        replacement = ([self._normalise_row(r, complete=True) for r in value]
                       if isinstance(index, slice) else self._normalise_row(value, complete=True))
        def assign(rows, columns):
            rows[index] = replacement
        self._mutate(assign)

    def __delitem__(self, index):
        def remove(rows, columns):
            del rows[index]
        self._mutate(remove)

    def insert(self, index, value):
        row = self._normalise_row(value, complete=True)
        self._mutate(lambda rows, columns: rows.insert(index, row))

    def pop(self, index=-1):
        return self._mutate(lambda rows, columns: rows.pop(index))

    def clear(self):
        self.delete()

    def sort(self, *, key=None, reverse=False):
        self._mutate(lambda rows, columns: _sort_rows(rows, [(key, 'DESC' if reverse else 'ASC')] if key else None))

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

    def key_value(self, key_column=None):
        return RedisTableKeyValue(self, key_column=key_column)

    @property
    def kv(self):
        return self.key_value()

    def read_pipeline(self):
        return self.kv.read_pipeline()

    def transaction(self):
        return RedisTableWriteBatch(self)

    def close(self):
        pass

    # Reuse the existing SQL grammar, including its validation and aliases.
    execute = _SQLTable.execute
    _clauses = staticmethod(_SQLTable._clauses)
    _assert_table = _SQLTable._assert_table
    _execute_select = _SQLTable._execute_select
    _execute_update = _SQLTable._execute_update
    _execute_delete = _SQLTable._execute_delete
    _execute_insert = _SQLTable._execute_insert

    def _read_snapshot_columns(self):
        return list(self.columns)


class RedisTableKeyValue(MutableMapping):
    def __init__(self, table, *, key_column=None):
        self.table = table
        self.key_columns = (key_column,) if key_column else table.primary_keys
        for field in self.key_columns:
            table.definition.column(field)

    def _normalise_key(self, key):
        return self.table._key_values(key, self.key_columns)

    def _public(self, key):
        values = self._normalise_key(key)
        return values[0] if len(values) == 1 else values

    def _where(self, key):
        return dict(zip(self.key_columns, self._normalise_key(key)))

    def _query(self, key):
        return self.table._query(self._where(key))

    def require(self, key, *, fields=None):
        rows = self.table._read([self._query(key)])[1][0]
        if not rows:
            raise KeyError(key)
        if len(rows) != 1:
            raise ValueError('key is not unique')
        row = rows[0]
        if fields is not None:
            fields = [fields] if isinstance(fields, str) else fields
            return {f: row.get(f) for f in fields}
        return row

    def get(self, key, default=None, *, fields=None):
        try:
            return self.require(key, fields=fields)
        except KeyError:
            return default

    def contains(self, key):
        return self.get(key) is not None

    __contains__ = contains

    def _patch(self, key, values):
        patch = self.table._normalise_row(values)
        for field, value in self._where(key).items():
            if field in patch and patch[field] != value:
                raise ValueError('key mutation is not supported')
        return patch

    def update(self, key, values=None, *, upsert=False):
        if values is None:
            return self.update_many(key)
        patch = self._patch(key, values)
        cmd = {'query': self._query(key), 'strict': True, 'fields': self.table._fields(patch)}
        if upsert:
            row = self.table._normalise_row({**patch, **self._where(key)}, complete=True)
            cmd['upsert'] = self.table._spec(row)
        return self.table._commands('patch', [cmd])

    def set(self, key, row, *, upsert=False):
        replacement = self.table._normalise_row({**self._patch(key, row), **self._where(key)}, complete=True)
        if self.key_columns == self.table.primary_keys:
            cmd = {'query': self._query(key), 'strict': True, 'fields': self.table._fields(replacement)}
            if upsert:
                cmd['upsert'] = self.table._spec(replacement)
            return self.table._commands('patch', [cmd])
        def replace(rows, columns):
            found = [i for i,r in enumerate(rows) if all(r[k] == v for k,v in self._where(key).items())]
            if len(found) > 1:
                raise ValueError('key is not unique')
            if not found:
                if not upsert:
                    raise KeyError(key)
                rows.append(replacement)
            else:
                rows[found[0]] = replacement
            return 1
        return self.table._mutate(replace)

    def get_many(self, keys):
        keys = [self._public(key) for key in keys]
        if not keys:
            return {}
        groups = self.table._read([self._query(key) for key in keys])[1]
        result = {}
        for key, rows in zip(keys, groups):
            if len(rows) > 1:
                raise ValueError('key is not unique')
            if rows:
                result[key] = rows[0]
        return result

    def update_many(self, updates):
        source = list(updates.items() if isinstance(updates, Mapping) else updates)
        seen, commands = set(), []
        for key, patch in source:
            key = self._public(key)
            if key in seen:
                raise ValueError('duplicate key in update_many')
            seen.add(key)
            commands.append({'query': self._query(key), 'strict': True,
                             'fields': self.table._fields(self._patch(key, patch))})
        return self.table._commands('patch', commands)

    def delete_many(self, keys):
        keys = [self._public(k) for k in keys]
        if len(set(keys)) != len(keys):
            raise ValueError('duplicate key in delete_many')
        return self.table._commands('delete', [{'query': self._query(k), 'strict': True} for k in keys])

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
        return [k for k,r in self.items()]

    def values(self):
        return [r for k,r in self.items()]

    def __iter__(self):
        return iter(self.keys())

    def __len__(self):
        return len(self.table) if self.key_columns == self.table.primary_keys else len(self.keys())

    __getitem__ = require
    __delitem__ = delete

    def __setitem__(self, key, value):
        self.set(key, value, upsert=True)

    def read_pipeline(self):
        return RedisTableReadPipeline(self)


class RedisTableReadPipeline:
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
        flat = [key for kind,value in commands for key in (value if kind == 'get_many' else [value])]
        rows = self.view.get_many(flat)
        result = []
        for kind,value in commands:
            if kind == 'get_many':
                keys = [self.view._public(k) for k in value]
                result.append({k: copy.deepcopy(rows[k]) for k in keys if k in rows})
            elif kind == 'contains':
                result.append(self.view._public(value) in rows)
            else:
                result.append(copy.deepcopy(rows.get(self.view._public(value))))
        return result


class RedisTableWriteBatch:
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
        if limit is not None or self.table._query(where) is None:
            raise ValueError('batch requires equality without LIMIT')
        return self._queue('patch', (where, self.table._normalise_row(values)))

    def update_by_pk(self, key, values):
        return self.update(values, self.table.kv._where(key))

    def delete(self, where, *, limit=None):
        if limit is not None or self.table._query(where) is None:
            raise ValueError('batch requires equality without LIMIT')
        return self._queue('delete', where)

    def delete_by_pk(self, key):
        return self.delete(self.table.kv._where(key))

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
        if kind == 'patch':
            return self.table.update_many_where(values)
        return self.table.delete_many_where(values)

    def rollback(self):
        self._operations.clear(); self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if not self._closed:
            self.commit() if kind is None else self.rollback()


class RedisDatabase:
    def __init__(self, directory=None, *, redis_url=None, host='127.0.0.1', port=6379,
                 db=0, prefix='rtdb', schema_directory=None, client=None,
                 socket_timeout=10.0, transaction_retries=16, wait_replicas=0,
                 wait_timeout_ms=1000, check_connection=True, durability='always'):
        if redis is None or msgpack is None:
            raise ImportError('install redis>=5 and msgpack>=1 for RedisDatabase')
        if durability not in ('always', 'server'):
            raise ValueError('durability must be always or server')
        if transaction_retries < 1 or wait_replicas < 0 or wait_timeout_ms <= 0:
            raise ValueError('invalid retry/replica timeout settings')
        _validate_identifier(prefix, label='prefix')
        self._owns_client = client is None
        if client is not None:
            retry = client.connection_pool.connection_kwargs.get('retry')
            if retry is not None and retry.get_retries() != 0:
                raise ValueError('supplied client must disable transport retries for ambiguous writes')
        if client is None:
            options = dict(decode_responses=False, socket_timeout=socket_timeout,
                           socket_connect_timeout=socket_timeout, retry=Retry(NoBackoff(), 0))
            client = (redis.Redis.from_url(redis_url, **options) if redis_url else
                      redis.Redis(host=host, port=port, db=db, **options))
        if client.connection_pool.connection_kwargs.get('decode_responses'):
            raise ValueError('Redis client must use decode_responses=False')
        self.redis = client
        self.directory = Path(directory).expanduser().resolve() if directory is not None else Path.cwd() / 'dat'
        identity = os.path.normcase(str(self.directory))
        digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.namespace = prefix + ':{' + digest + '}'
        self.transaction_retries = transaction_retries
        self.wait_replicas, self.wait_timeout_ms = wait_replicas, wait_timeout_ms
        self.schema_directory = Path(schema_directory) if schema_directory else self.directory.parent / 'def' / 'table'
        self.schema_registry = SchemaRegistry(self.schema_directory)
        self._tables, self._lock = {}, threading.RLock()
        if check_connection:
            self.redis.ping()
        if durability == 'always':
            config = self.server_config()
            required = {'appendonly': 'yes', 'appendfsync': 'always',
                        'no-appendfsync-on-rewrite': 'no', 'maxmemory-policy': 'noeviction'}
            if any(config.get(k) != v for k,v in required.items()):
                raise RedisStorageError(f'durable Redis requires {required}; actual {config}')
        self.durability = durability

    def server_config(self):
        names = ('appendonly', 'appendfsync', 'no-appendfsync-on-rewrite', 'maxmemory-policy', 'maxmemory', 'save')
        result = {}
        for name in names:
            result.update({_text(k): _text(v) for k,v in self.redis.config_get(name).items()})
        return result

    def table(self, name):
        name = _validate_identifier(name, label='table name')
        with self._lock:
            if name not in self._tables:
                self._tables[name] = RedisTable(self, self.schema_registry.load(name))
            return self._tables[name]
    __getitem__ = table
    execute = FileMappedDatabase.execute

    def refresh_schema(self):
        with self._lock:
            self.schema_registry.clear(); self._tables.clear()

    def close(self):
        with self._lock:
            self._tables.clear()
            if self._owns_client:
                self.redis.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        self.close()
