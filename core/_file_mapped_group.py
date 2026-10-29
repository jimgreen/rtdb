"""Private, touched-slot staging for the fixed-table writer process.

Nothing here publishes mmap bytes or invents a second durability protocol.
The caller holds the existing table lock through validation and _commit_locked.
"""
import copy
import math
from collections.abc import Mapping

from .file_mapped_db import (
    SchemaError, _FIXED_SLOT_HEADER, _MAX_WAL_RECORD,
    _FIXED_WAL_PAYLOAD_HEADER, _FIXED_WAL_ENTRY_HEADER, _parse_equality_mapping,
)

_MISSING = object()


class FixedWriteGroup:
    def __init__(self, table, mapping, header):
        self.table, self.store, self.mapping, self.header = table, table._fixed_store, mapping, header
        self.entries, self.originals, self.indexes, self.buckets = {}, {}, {}, {}
        self.count = header['row_count']
        self.cursor = self.count % self.store.max_records
        self.insertions = 0
        self.undo = []

    def _set(self, target, key, value):
        self.undo.append((target, key, target.get(key, _MISSING)))
        target[key] = value

    def _original(self, slot):
        if slot not in self.originals:
            offset = self.store._slot_offset(slot)
            raw = self.mapping[offset:offset + self.store.record_size]
            self.store._decode_record(raw, fields=())  # CRC before trusting bytes
            self.originals[slot] = raw
        return self.originals[slot]

    def _record(self, slot):
        return self.entries[slot] if slot in self.entries else self._original(slot)

    def _key(self, raw, columns):
        if not raw[0]:
            return None
        fields = [self.store._fields_by_name[c] for c in columns]
        payload = raw[_FIXED_SLOT_HEADER.size:]
        return tuple(self.store._decode_field(payload[f.offset:f.offset + f.width], f) for f in fields)

    def _index(self, columns):
        if columns not in self.indexes:
            index = self.store._key_index_locked(self.mapping, self.header['generation'], columns)
            self.indexes[columns], self.buckets[columns] = index, {}
            # A condition first encountered after earlier requests must see
            # their staged changes as well. Rebuild ONLY the touched buckets.
            for slot, raw in self.entries.items():
                self._change_bucket(columns, slot, self._original(slot), raw, record_undo=False)
        return self.indexes[columns]

    def _change_bucket(self, columns, slot, old, new, *, record_undo=True):
        old_key, new_key = self._key(old, columns), self._key(new, columns)
        if old_key == new_key and (old_key is None or
                _FIXED_SLOT_HEADER.unpack_from(old)[1] == _FIXED_SLOT_HEADER.unpack_from(new)[1]):
            return
        bucket, base = self.buckets[columns], self.indexes[columns]
        for key, adding in ((old_key, False), (new_key, True)):
            if key is None:
                continue
            hash(key)
            values = bucket.get(key, base.get(key, ()))
            values = (*values, slot) if adding else tuple(i for i in values if i != slot)
            if record_undo:
                self._set(bucket, key, values)
            else:
                bucket[key] = values

    def _positions(self, columns, key, limit=None):
        self._index(columns)
        hash(key)
        if key in self.buckets[columns]:
            slots = self.buckets[columns][key]
            slots = sorted(slots, key=lambda slot: _FIXED_SLOT_HEADER.unpack_from(self._record(slot))[1])
        else:
            slots = self.indexes[columns].get(key, ())
        return slots if limit is None else slots[:limit]

    def _stage(self, slot, raw):
        old = self._record(slot)
        for columns in self.indexes:
            self._change_bucket(columns, slot, old, raw)
        self._set(self.entries, slot, raw)

    def _free(self):
        for _ in range(self.store.max_records):
            slot = self.cursor
            self.cursor = (slot + 1) % self.store.max_records
            state = (self.entries[slot][0] if slot in self.entries
                     else self.mapping[self.store._slot_offset(slot)])
            if state == 0:
                return slot
        raise SchemaError('table has no free slots')

    def _insert(self, rows):
        primary = self.store.definition.primary_keys
        count = 0
        for source in rows:
            if not isinstance(source, Mapping):
                raise TypeError('rows must be mappings')
            row = copy.deepcopy(dict(source))
            self.store._validate_row(row)
            self._validate_finite(row)
            if self.count >= self.store.max_records:
                raise SchemaError('table capacity exceeded')
            order = self.store._order_for_generation(self.header['generation'] + 1, self.insertions)
            raw = self.store._encode_record(row, order)
            # Compare the stored representation (e.g. float rounding), not
            # a source key that may change when encoded into its fixed field.
            key = self._key(raw, primary)
            if self._positions(primary, key):
                raise SchemaError(f'duplicate primary key {key!r}')
            self._stage(self._free(), raw)
            self.count += 1
            self.insertions += 1
            count += 1
        return count

    def _patch(self, values):
        if not isinstance(values, Mapping):
            raise TypeError('patch must be a mapping')
        patch = copy.deepcopy(dict(values))
        self.store._validate_patch(patch)
        self._validate_finite(patch)
        return patch

    @staticmethod
    def _validate_finite(values):
        for name, value in values.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise SchemaError(f'column {name!r} requires a finite value')

    def _change(self, columns, key, patch, *, strict=False, limit=None, deleting=False):
        slots = self._positions(columns, key)
        if strict:
            if not slots:
                raise KeyError(key[0] if len(key) == 1 else key)
            if len(slots) != 1:
                raise ValueError('key is not unique')
        slots = list(slots if limit is None else slots[:limit])
        primary = self.store.definition.primary_keys
        for slot in slots:
            if deleting:
                self._stage(slot, bytes(self.store.record_size))
                self.count -= 1
            else:
                old = self._record(slot)
                _, raw = self.store._patch_record(old, patch)
                if set(patch).intersection(primary):
                    new_key = self._key(raw, primary)
                    if any(i != slot for i in self._positions(primary, new_key)):
                        raise SchemaError(f'duplicate primary key {new_key!r}')
                if old != raw:
                    self._stage(slot, raw)
        return len(slots)

    def _condition(self, where):
        where = dict(where) if isinstance(where, Mapping) else _parse_equality_mapping(where)
        if not where:
            raise ValueError('writer conditions require non-empty scalar equalities')
        for column, value in where.items():
            if column not in self.store._fields_by_name:
                raise SchemaError(f'unknown column: {column}')
            if (value is not None and not isinstance(value, (str, int, float, bool)) or
                    isinstance(value, float) and not math.isfinite(value)):
                raise ValueError('writer conditions require finite scalar values')
        return tuple(where), tuple(where.values())

    def _execute(self, operation, payload):
        if operation == 'insert':
            return self._insert(payload)
        if operation in ('update', 'delete'):
            patch, where, limit = payload if operation == 'update' else (None, *payload)
            if limit is not None and (not isinstance(limit, int) or limit < 0):
                raise ValueError('limit must be a non-negative integer')
            columns, key = self._condition(where)
            return self._change(columns, key, self._patch(patch) if patch is not None else None,
                                limit=limit, deleting=operation == 'delete')
        deleting = operation in ('delete_pk', 'delete_key', 'delete_many_pk', 'delete_many_key')
        key_column = None
        if operation in ('update_pk', 'update_key'):
            items = [payload[:2]]
            key_column = payload[2] if operation == 'update_key' else None
        elif operation in ('delete_pk', 'delete_key'):
            items = [payload if operation == 'delete_pk' else payload[0]]
            key_column = payload[1] if operation == 'delete_key' else None
        elif operation in ('update_many_pk', 'delete_many_pk'):
            items = payload
        elif operation in ('update_many_key', 'delete_many_key'):
            items, key_column = payload
        else:
            raise ValueError(f'unsupported writer operation: {operation}')
        view = self.table.key_value(key_column)
        seen, count = set(), 0
        for item in items:
            key = view._normalise_key(item if deleting else item[0], for_write=False)
            if key in seen:
                raise ValueError('duplicate key within one batch request')
            seen.add(key)
            patch = None if deleting else self._patch(item[1])
            # Primary update_by_pk keeps the existing Table API's key-change
            # support; explicit key-value operations retain immutable keys.
            if not deleting and operation not in ('update_pk',):
                view._reject_key_patch(patch, key)
            count += self._change(view.key_columns, key, patch, strict=True, deleting=deleting)
        return count

    def apply(self, operation, payload):
        """One request savepoint; no staged bytes have reached mmap/WAL yet."""
        self.undo = []
        old_count, old_cursor, old_insertions = self.count, self.cursor, self.insertions
        old_indexes = set(self.indexes)
        try:
            result = self._execute(operation, payload)
            size = _FIXED_WAL_PAYLOAD_HEADER.size + len(self.entries) * (
                _FIXED_WAL_ENTRY_HEADER.size + self.store.record_size)
            if size > _MAX_WAL_RECORD:
                raise ValueError('staged WAL exceeds maximum frame size; reduce request/batch size')
            return result
        except Exception:
            for target, key, previous in reversed(self.undo):
                if previous is _MISSING:
                    target.pop(key, None)
                else:
                    target[key] = previous
            # New indexes were initialized against this request's intermediate
            # edits. Discard that temporary bucket view on rollback.
            for columns in set(self.indexes) - old_indexes:
                del self.indexes[columns]
                del self.buckets[columns]
            self.count, self.cursor, self.insertions = old_count, old_cursor, old_insertions
            raise
        finally:
            self.undo = []

    def commit(self):
        if not self.entries:
            return False
        store, header = self.store, self.header
        entries = [(slot, raw) for slot, raw in self.entries.items() if raw != self._original(slot)]
        if not entries:
            return False
        inserts = all(not self._original(slot)[0] and raw[0] for slot, raw in entries)
        deletes = all(self._original(slot)[0] and not raw[0] for slot, raw in entries)
        updates = all(self._original(slot)[0] and raw[0] for slot, raw in entries)
        reordered = any(self._original(slot)[0] and raw[0] and
                        _FIXED_SLOT_HEADER.unpack_from(self._original(slot))[1] !=
                        _FIXED_SLOT_HEADER.unpack_from(raw)[1] for slot, raw in entries)
        changed = set()
        if updates:
            for slot, raw in entries:
                before = self._original(slot)
                for field in store.fields:
                    start = _FIXED_SLOT_HEADER.size + field.offset
                    if raw[start:start + field.width] != before[start:start + field.width]:
                        changed.add(field.name)
        with store._key_cache_guard.read():
            cached = {c: index for c, (generation, index) in store._key_cache.items()
                      if generation == header['generation']}
        additions = {c: [(self._key(raw, c), slot) for slot, raw in entries] for c in cached} if inserts else None
        preserve = inserts or deletes or updates and not reordered and all(not set(c).intersection(changed) for c in cached)
        # Many insert/delete cycles in a tiny table can consume more than one
        # insertion-order stride. Reserve the required generations explicitly.
        generation = header['generation'] + 1 + max(0, self.insertions - 1) // (store.max_records + 1)
        store._commit_locked(entries, self.count, generation, header=header,
            preserve_key_cache=preserve,
            key_cache_additions=additions,
            key_cache_removals=[slot for slot, raw in entries] if deletes else None,
            persisted_index_operation='add' if inserts else 'remove' if deletes else None,
            persisted_index_changed_columns=changed if updates and not reordered else None)
        if preserve:
            return True
        # General mixed/key-changing groups invalidate disposable caches. Keep
        # the writer's loaded indexes current by changing ONLY affected buckets;
        # external readers still use the authoritative generation/CRC gates.
        with store._key_cache_guard:
            for columns, base in self.indexes.items():
                buckets = self.buckets[columns]
                if not buckets:
                    if updates and set(columns).intersection(changed):
                        continue  # loaded after a changed row? conservatively rebuild
                    store._key_cache[columns] = (generation, base)
                    continue
                index = dict(base)
                for key, slots in buckets.items():
                    if slots:
                        index[key] = tuple(sorted(slots, key=lambda slot:
                            _FIXED_SLOT_HEADER.unpack_from(self._record(slot))[1]))
                    else:
                        index.pop(key, None)
                store._key_cache[columns] = (generation, index)
        return True
