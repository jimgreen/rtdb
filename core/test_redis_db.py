"""Real Redis integration tests. Set RTDB_TEST_REDIS_URL; never FLUSHDB.

Each test owns a random path-derived namespace and removes only that namespace.
fakeredis lacks Redis's built-in cmsgpack and is not a substitute for these tests.
"""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from .redis_db import RedisDatabase, RedisDurabilityError
from .table_schema import TableDefinition, SchemaError

URL = os.environ.get('RTDB_TEST_REDIS_URL')


class RedisPublicInterfaceTests(unittest.TestCase):
    def test_durability_error_is_exported(self):
        from core import RedisDurabilityError as exported
        self.assertIs(exported, RedisDurabilityError)


@unittest.skipUnless(URL, 'set RTDB_TEST_REDIS_URL to run real Redis integration tests')
class RedisBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rtdb-test-redis-')
        self.root = Path(self.temp.name)
        self.schema = self.root / 'def'
        self._definition('records')
        self.db = self.open()
        self.table = self.db['records']

    def open(self, **kw):
        return RedisDatabase(self.root / 'dat', redis_url=URL, schema_directory=self.schema, **kw)

    def tearDown(self):
        keys = list(self.db.redis.scan_iter(match=self.db.namespace + ':*'))
        if keys:
            self.db.redis.delete(*keys)
        self.db.close()
        self.temp.cleanup()

    def _definition(self, name, *, composite=False):
        fields = [('id', True, 'int')]
        if composite:
            fields = [('site', True, 'str'), ('device', True, 'int')]
        fields += [('group', False, 'int'), ('value', False, 'int')]
        definition = TableDefinition.from_mapping({
            'head': {'chn_name': name, 'eng_name': name},
            'columns': [{'chn_name': f, 'eng_name': f, 'primary_key': pk,
                         'data_type': typ,
                         'disp_type': 'text', 'disp_menu': ''} for f,pk,typ in fields],
        }, self.schema, name)
        definition.to_json(self.schema, overwrite=True)
        return definition

    def seed(self, n=3):
        self.table.insert_many({'id': i, 'group': i % 2, 'value': i} for i in range(n))

    def test_crud_and_secondary_index_maintenance(self):
        self.seed()
        self.assertEqual(self.table.update({'group': 9}, {'group': 0}, limit=1), 1)
        self.assertEqual(self.table.select({'group': 9})[0]['id'], 0)
        self.assertEqual([r['id'] for r in self.table.select({'group': 0})], [2])
        self.table.delete_by_pk(0)
        self.assertEqual(self.table.select({'group': 9}), [])
        self.assertEqual(len(self.table), 2)

    def test_composite_and_large_integer_keys(self):
        self._definition('pairs', composite=True)
        table = self.db['pairs']
        value = 2**60 + 3
        table.append({'site': '站点甲', 'device': value, 'group': 1, 'value': value})
        self.assertEqual(table.kv.get(['站点甲', value])['value'], value)
        self.assertIsNone(table.kv.get(('站点甲', value + 1)))
        self.assertIn(('站点甲', value), table.kv.get_many([['站点甲', value]]))
        with table.transaction() as tx:
            tx.update_by_pk(('站点甲', value), {'value': 9})
        self.assertEqual(table.get_by_pk(('站点甲', value))['value'], 9)

    def test_list_duplicate_failure_is_atomic(self):
        self.seed()
        before = self.table.all()
        with self.assertRaises(ValueError):
            self.table[0] = {'id': 1, 'group': 1, 'value': 9}
        self.assertEqual(self.table.all(), before)
        with self.assertRaises(ValueError):
            self.table.insert(1, {'id': 1, 'group': 1, 'value': 1})
        self.assertEqual(self.table.all(), before)

    def test_list_slices_order_and_pop(self):
        self.seed()
        self.table[0] = {'id': 8, 'group': 1, 'value': 8}
        self.assertEqual([r['id'] for r in self.table], [8, 1, 2])
        self.assertEqual(self.table[0:0], [])
        self.table[1:2] = [{'id': 9, 'group': 1, 'value': 9}]
        self.table.insert(1, {'id': 7, 'group': 1, 'value': 7})
        self.assertEqual([r['id'] for r in self.table], [8, 7, 9, 2])
        self.assertEqual(self.table.pop(1)['id'], 7)
        self.table.sort(key='id', reverse=True)
        self.assertEqual([r['id'] for r in self.table], [9, 8, 2])
        self.assertEqual([r['id'] for r in self.table.select({'group': 1})], [9, 8])
        del self.table[::-2]
        self.assertEqual([r['id'] for r in self.table], [8])

    def test_batch_missing_keys_and_duplicate_rollback(self):
        self.seed()
        before = self.table.all()
        with self.assertRaises(KeyError):
            self.table.kv.delete_many([0, 99])
        with self.assertRaises(KeyError):
            self.table.kv.update_many([(0, {'value': 8}), (99, {'value': 1})])
        with self.assertRaises(ValueError):
            self.table.insert_many([{'id': 88, 'group': 1, 'value': 1}, {'id': 0, 'group': 1, 'value': 1}])
        self.assertEqual(self.table.all(), before)
        self._definition('small')
        small = self.db['small']
        small.append({'id': 1, 'group': 1, 'value': 1})
        small.append({'id': 2, 'group': 1, 'value': 1})
        self.assertEqual(small.kv.keys(), [1, 2])

    def test_upsert_and_detached_pipeline(self):
        self.table.kv.update(1, {'group': 1, 'value': 2}, upsert=True)
        self.table.kv[1] = {'value': 9}
        self.assertEqual(self.table.kv.get(1), {'id': 1, 'group': None, 'value': 9})
        self.assertEqual(len(self.table.select({'group': None})), 1)
        rows = self.table.read_pipeline().get(1).get(1).contains(9).get_many([1]).execute()
        rows[0]['value'] = -1
        self.assertEqual(rows[1]['value'], 9)
        self.assertFalse(rows[2])
        self.assertEqual(rows[3][1]['value'], 9)

    def test_transaction_rollback_and_sequential_conditions(self):
        self.seed()
        before = self.table.all()
        with self.assertRaises(RuntimeError):
            with self.table.transaction() as batch:
                batch.update_by_pk(0, {'value': 8})
                raise RuntimeError('cancel')
        self.assertEqual(self.table.all(), before)
        with self.assertRaises(ValueError):
            with self.table.transaction() as batch:
                batch.update_by_pk(0, {'value': 8})
                batch.insert({'id': 7, 'group': 1, 'value': 1})
        self.assertEqual(self.table.all(), before)
        self.table.update_many_where([({'id': 0}, {'group': 99}), ({'group': 99}, {'value': 100})])
        self.assertEqual(self.table.kv.get(0)['value'], 100)
        self.assertEqual(self.table.delete_many_where([{'id': 0}, {'group': 99}]), 1)

    def test_sql_predicate_sort_projection_and_insert(self):
        self.db.execute('INSERT INTO records (id,group,value) VALUES (1,2,3), (2,2,5)')
        self.assertEqual(self.db.execute('SELECT id AS number FROM records WHERE value > 3'), [{'number': 2}])
        self.assertEqual(self.table.update({'value': 9}, lambda r: r['id'] == 2), 1)
        self.assertEqual(self.table.execute('SELECT * FROM records ORDER BY id DESC LIMIT 1')[0]['value'], 9)
        self.assertEqual(self.db.execute('DELETE FROM records WHERE id = 1').rowcount, 1)

    def test_secondary_unique_key(self):
        self.seed()
        view = self.table.key_value('value')
        self.assertEqual(view.get(1)['id'], 1)
        view.update(1, {'group': 9})
        self.assertEqual(self.table.kv.get(1)['group'], 9)
        self.table.update_by_pk(2, {'value': 1})
        with self.assertRaises(ValueError):
            view.update(1, {'group': 8})

    def test_threaded_patches_and_snapshot_batch_reads(self):
        self.seed(2)
        def write(field):
            for n in range(10):
                self.table.update_by_pk(0, {field: n})
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(write, ('group', 'value')))
        self.assertEqual(self.table.kv.get(0), {'id': 0, 'group': 9, 'value': 9})
        self.table.kv.update_many({0: {'value': 0}, 1: {'value': 0}})
        def writer():
            for n in range(20):
                self.table.kv.update_many({0: {'value': n}, 1: {'value': n}})
        with ThreadPoolExecutor(1) as pool:
            task = pool.submit(writer)
            for _ in range(40):
                rows = self.table.read_pipeline().get(0).get(1).execute()
                self.assertEqual(rows[0]['value'], rows[1]['value'])
            task.result()

    def test_already_open_client_hot_sync(self):
        other = self.open()
        try:
            mirror = other['records']
            self.seed(1)
            self.assertEqual(mirror.kv.get(0)['value'], 0)
            self.table.update_by_pk(0, {'value': 99})
            self.assertEqual(mirror.kv.get(0)['value'], 99)
            self.table.delete_by_pk(0)
            self.assertIsNone(mirror.kv.get(0))
        finally:
            other.close()

    def test_schema_drift_rejected(self):
        self.seed()
        import json
        path = self.schema / 'records.json'
        definition = json.loads(path.read_text(encoding='utf-8'))
        definition['columns'][1]['data_type'] = 'str'
        path.write_text(json.dumps(definition), encoding='utf-8')
        with self.open() as db:
            with self.assertRaises(SchemaError):
                db.table('records')

    def test_wait_uses_same_connection_and_reports_missing_ack(self):
        import redis
        self.db.wait_replicas = 1
        self.db.wait_timeout_ms = 1
        script = self.table._script
        wait = redis.Redis.wait
        connections = []
        def invoke(*args, **kw):
            connections.append(kw['client'].connection)
            return script(*args, **kw)
        def checked_wait(client, *args, **kw):
            self.assertIsNotNone(client.connection)
            self.assertIs(client.connection, connections[-1])
            return wait(client, *args, **kw)
        with patch.object(self.table, '_script', invoke), patch.object(redis.Redis, 'wait', checked_wait):
            with self.assertRaises(RedisDurabilityError):
                self.table.append({'id': 1, 'group': 1, 'value': 1})
        self.assertEqual(len(self.table), 1)
        self.db.wait_replicas = 0

    def test_wrong_type_preflight_leaves_batch_unchanged(self):
        import redis
        from .redis_db import _token
        self.seed(1)
        index = self.table.base + ':idx:group:' + _token(98)
        self.db.redis.set(index, 'invalid index type')
        with self.assertRaises(redis.exceptions.ResponseError):
            self.table.insert_many([{'id': 5, 'group': 7}, {'id': 6, 'group': 98}])
        self.assertEqual(self.table.kv.keys(), [0])

    def test_duplicate_batches_and_primary_change(self):
        self.seed()
        before = self.table.all()
        for action in (
            lambda: self.table.kv.update_many([(0, {'value': 4}), (0, {'value': 5})]),
            lambda: self.table.kv.delete_many([0, 0]),
            lambda: self.table.update_many_where([({'id': 0}, {'value': 9}), ({'id': 1}, {'id': 8})]),
        ):
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(self.table.all(), before)

    def test_numeric_equality_and_empty_limits(self):
        self.seed()
        self.assertEqual(self.table.kv.get(1.0)['id'], 1)
        self.assertEqual(self.table.select({'group': True})[0]['id'], 1)
        self.assertEqual(self.table.select({'group': 0}, offset=1, limit=1)[0]['id'], 2)
        self.assertEqual(self.table.select(limit=0), [])
        self.assertEqual(self.table.delete(limit=0), 0)
        self.assertEqual(len(self.table), 3)

    def test_multiple_equalities_apply_limit_after_intersection(self):
        # Both indexes have nonmatching entries before the intersection. A
        # candidate LIMIT applied to either index would lose ids 4 and 5.
        self.table.insert_many([
            {'id': 0, 'group': 1, 'value': 0},
            {'id': 1, 'group': 1, 'value': 0},
            {'id': 2, 'group': 0, 'value': 9},
            {'id': 3, 'group': 0, 'value': 9},
            {'id': 4, 'group': 1, 'value': 9},
            {'id': 5, 'group': 1, 'value': 9},
        ])
        for where in ({'group': 1, 'value': 9}, {'value': 9, 'group': 1},
                      'group = 1 AND value = 9'):
            with self.subTest(where=where):
                self.assertEqual(self.table.find(where)['id'], 4)
                self.assertEqual([r['id'] for r in self.table.select(where, limit=1)], [4])
                self.assertEqual([r['id'] for r in self.table.select(where, offset=1, limit=1)], [5])
                self.assertEqual(self.table.select(where, offset=2, limit=1), [])
        self.assertEqual(self.table.update({'value': 8}, {'group': 1, 'value': 9}, limit=1), 1)
        self.assertEqual(self.table.get_by_pk(4)['value'], 8)
        self.assertEqual(self.table.delete({'group': 1, 'value': 9}, limit=1), 1)
        self.assertEqual(self.table.kv.keys(), [0, 1, 2, 3, 4])

    def test_batch_conditions_see_staged_index_changes_in_physical_order(self):
        self.seed(6)
        self.table.update_many_where([
            ({'id': 1}, {'group': 9, 'value': 9}),
            ({'id': 4}, {'group': 9, 'value': 9}),
            ({'group': 9, 'value': 9}, {'value': 10}),
        ])
        self.assertEqual([r['id'] for r in self.table.select({'group': 9, 'value': 10})], [1, 4])
        self.assertEqual(self.table.select({'group': 9, 'value': 9}), [])
        self.assertEqual(self.table.delete_many_where([
            {'id': 1}, {'group': 9, 'value': 10}, {'group': 9},
        ]), 2)
        self.assertEqual(self.table.kv.keys(), [0, 2, 3, 5])

    def test_large_intersections_preserve_pagination_and_write_targets(self):
        self._definition('intersections')
        table = self.db['intersections']
        rows = [{'id': i, 'group': i // 300, 'value': i % 2} for i in range(600)]
        table.insert_many(rows)
        for where in ({'value': 0, 'group': 1}, {'group': 1, 'value': 0}):
            for offset, limit in ((0, 1), (1, 2), (100, 10), (130, 10), (150, 1)):
                with self.subTest(where=where, offset=offset, limit=limit):
                    expected = [r for r in rows if r['group'] == 1 and r['value'] == 0][offset:][:limit]
                    self.assertEqual(table.select(where, offset=offset, limit=limit), expected)
        self.assertEqual(table.update({'value': 2}, {'value': 0, 'group': 1}, limit=1), 1)
        self.assertEqual(table.get_by_pk(300)['value'], 2)
        self.assertEqual(table.delete({'value': 0, 'group': 1}, limit=1), 1)
        self.assertIsNone(table.kv.get(302))
        # A physical list reorder must also reorder both equality indexes.
        table.sort(key='id', reverse=True)
        self.assertEqual(table.find({'value': 0, 'group': 1})['id'], 598)
        table.update_many_where([({'id': 0}, {'group': 1}), ({'group': 1, 'value': 0}, {'value': 3})])
        self.assertEqual(table.get_by_pk(0)['value'], 3)
        self.assertEqual(table.select({'group': 1, 'value': 0}, limit=1), [])

    def test_ordered_and_single_index_pagination_after_deletes_and_reorder(self):
        self.seed(60)
        self.table.kv.delete_many([1, 4, 9, 20])
        for reverse in (False, True):
            if reverse:
                self.table.sort(key='id', reverse=True)
            rows = self.table.all()
            for where in (None, {'group': 0}, {'group': 1}, {'id': 30}):
                selected = [r for r in rows if all(r[k] == v for k,v in (where or {}).items())]
                for offset, limit in ((0, 1), (3, 7), (20, 1), (20, None), (500, 1), (3, 0)):
                    with self.subTest(reverse=reverse, where=where, offset=offset, limit=limit):
                        self.assertEqual(self.table.select(where, offset=offset, limit=limit),
                                         selected[offset:][:limit])
            self.assertEqual(self.table[30], rows[30])


if __name__ == '__main__':
    unittest.main()
