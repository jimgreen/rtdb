"""SQLite integration tests use real files, connections and spawned processes."""
import multiprocessing as mp
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from core.sqlite_db import SQLiteDatabase, SQLiteBusyError, SQLiteWriteProcess
from core.table_schema import SchemaError
from benchmarks.benchmark_redis_vs_file import _definition, _row


def append_worker(root, start):
    with SQLiteDatabase(root) as db:
        for i in range(start, start + 20):
            db['records'].append(_row(i))


def dedicated_append_worker(client, start):
    for i in range(start, start + 25):
        client.append('records', _row(i))
    client.close()


def crash_writer(root, ready):
    db = SQLiteDatabase(root)
    table = db['records']
    table.update_by_pk(0, {'value': 999})
    table.append(_row(1000))
    table.delete_by_pk(1)
    # Leave the following transaction uncommitted when the parent kills us.
    connection = table._connection()
    connection.execute('BEGIN IMMEDIATE')
    connection.execute('UPDATE data SET c2 = -1')
    ready.send(True)
    ready.recv()


class SQLiteBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rtdb-sqlite-test-')
        self.root = Path(self.temp.name) / 'dat'
        _definition(self.root)
        self.db = SQLiteDatabase(self.root)
        self.table = self.db['records']

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def seed(self, n=10):
        self.table.insert_many(_row(i) for i in range(n))

    def test_native_columns_config_indexes_and_hot_sync(self):
        self.seed()
        config = self.table.storage_config()
        self.assertEqual(config['journal_mode'], 'wal')
        self.assertEqual(config['synchronous'], 2)
        self.assertGreater(config['mmap_size'], 0)
        with SQLiteDatabase(self.root) as other:
            mirror = other['records']
            self.assertEqual(mirror.kv.get(1), _row(1))
            self.table.update_by_pk(1, {'value': 999})
            self.assertEqual(mirror.kv.get(1)['value'], 999)
            self.table.delete_by_pk(1)
            self.assertIsNone(mirror.kv.get(1))
        connection = self.table._connection()
        self.assertEqual(connection.execute('SELECT typeof(c0),typeof(c2),typeof(c3) FROM data LIMIT 1').fetchone(),
                         ('integer', 'integer', 'text'))
        self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        plan = connection.execute('EXPLAIN QUERY PLAN SELECT * FROM data WHERE c1 = 1').fetchall()
        self.assertTrue(any('INDEX' in r[-1] for r in plan))

    def test_float_boolean_and_structured_values_keep_types(self):
        import json
        path = self.root.parent / 'def/table/typed.json'
        payload = {
            'head': {'chn_name': 'typed', 'eng_name': 'typed'},
            'columns': [
                {'chn_name': 'id', 'eng_name': 'id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'text', 'disp_menu': ''},
                {'chn_name': 'ratio', 'eng_name': 'ratio', 'primary_key': False, 'data_type': 'float', 'disp_type': 'text', 'disp_menu': ''},
                {'chn_name': 'enabled', 'eng_name': 'enabled', 'primary_key': False, 'data_type': 'bool', 'disp_type': 'text', 'disp_menu': ''},
                {'chn_name': 'tags', 'eng_name': 'tags', 'primary_key': False, 'data_type': 'json', 'disp_type': 'text', 'disp_menu': ''},
            ],
        }
        path.write_text(json.dumps(payload), encoding='utf-8')
        table = self.db['typed']
        table.append({'id': 1, 'ratio': 2, 'enabled': True, 'tags': {'x': [1, 2]}})
        self.assertEqual(table.kv.get(1), {'id': 1, 'ratio': 2.0, 'enabled': True, 'tags': {'x': [1, 2]}})

    def test_crud_equality_projection_sort_and_sql(self):
        self.seed(30)
        self.assertEqual(self.table.update({'value': 777}, {'group': 1}, limit=1), 1)
        self.assertEqual(self.table.select({'group': 1}, columns=['id', 'value']),
                         [{'id': 1, 'value': 777}, {'id': 17, 'value': 34}])
        self.assertEqual(self.table.select({'group': 1}, offset=1, limit=1)[0]['id'], 17)
        self.assertEqual(self.db.execute('SELECT id AS number FROM records WHERE value > 50 ORDER BY id DESC LIMIT 2'),
                         [{'number': 29}, {'number': 28}])
        self.assertEqual(self.db.execute('UPDATE records SET value = 8 WHERE id = 2').rowcount, 1)
        self.assertEqual(self.db.execute("INSERT INTO records (id,group,value,name) VALUES (40,1,3,'new')").rowcount, 1)
        self.assertEqual(self.db.execute('DELETE FROM records WHERE id = 40').rowcount, 1)
        self.assertEqual(self.table.delete({'group': 1, 'value': 34}, limit=1), 1)
        self.assertEqual(self.table.count({'group': 1}), 1)

    def test_batch_rollback_and_sequential_conditions(self):
        self.seed()
        before = self.table.all()
        for action in (
            lambda: self.table.kv.update_many([(0, {'value': 9}), (99, {'value': 1})]),
            lambda: self.table.kv.delete_many([0, 99]),
            lambda: self.table.insert_many([_row(99), _row(0)]),
            lambda: self.table.update_many_where([({'id': 0}, {'value': 99}), ({'id': 1}, {'id': 0})]),
        ):
            with self.assertRaises((KeyError, ValueError)):
                action()
            self.assertEqual(self.table.all(), before)
        self.table.update_many_where([({'id': 0}, {'group': 99}), ({'group': 99}, {'value': 100})])
        self.assertEqual(self.table.kv.get(0)['value'], 100)
        self.assertEqual(self.table.delete_many_where([{'id': 0}, {'group': 99}]), 1)

    def test_list_reorder_and_atomic_duplicate_failure(self):
        self.seed(3)
        before = self.table.all()
        with self.assertRaises(ValueError):
            self.table[0] = _row(1)
        self.assertEqual(self.table.all(), before)
        self.table[1:2] = [_row(8), _row(9)]
        self.table.insert(1, _row(7))
        self.assertEqual([r['id'] for r in self.table], [0, 7, 8, 9, 2])
        self.assertEqual(self.table.pop(2)['id'], 8)
        self.table.sort(key='id', reverse=True)
        self.assertEqual([r['id'] for r in self.table], [9, 7, 2, 0])
        del self.table[::2]
        self.assertEqual(self.table.kv.keys(), [7, 0])
        self.assertEqual(self.table[-1]['id'], 0)

    def test_transactions_upsert_pipeline_and_detached_values(self):
        self.table.kv.update(1, {'name': 'one', 'value': 1}, upsert=True)
        self.table.kv[1] = {'value': 2}
        self.assertEqual(self.table.kv.get(1), {'id': 1, 'group': None, 'value': 2, 'name': None})
        with self.table.transaction() as batch:
            batch.update_by_pk(1, {'value': 9})
            batch.update({'group': 5}, {'value': 9})
        result = self.table.read_pipeline().get(1).get(1).contains(2).get_many([1, 2]).execute()
        result[0]['value'] = -1
        self.assertEqual(result[1]['value'], 9)
        self.assertFalse(result[2])
        before = self.table.all()
        with self.assertRaises(RuntimeError):
            with self.table.transaction() as batch:
                batch.delete_by_pk(1)
                raise RuntimeError('cancel')
        self.assertEqual(self.table.all(), before)

    def test_schema_drift_and_value_validation_without_definition_limits(self):
        import json
        path = self.root.parent / 'def/table/records.json'
        payload = json.loads(path.read_text())
        payload['head']['chn_name'] = '改名后的记录'
        path.write_text(json.dumps(payload), encoding='utf-8')
        with SQLiteDatabase(self.root) as other:
            with self.assertRaises(SchemaError):
                other['records']
        self.assertEqual(len(self.table), 0)
        self.table.append({**_row(1), 'name': 'x' * 333})
        self.assertEqual(self.table.kv.get(1)['name'], 'x' * 333)
        payload['head']['eng_name'] = 'small'
        (path.parent / 'small.json').write_text(json.dumps(payload), encoding='utf-8')
        small = self.db['small']
        small.append(_row(1))
        small.append(_row(2))
        self.assertEqual(len(small), 2)


    def test_composite_primary_and_large_integers(self):
        import json
        path = self.root.parent / 'def/table/records.json'
        payload = json.loads(path.read_text())
        payload['head']['eng_name'] = 'pairs'
        payload['columns'][1]['primary_key'] = True
        (path.parent / 'pairs.json').write_text(json.dumps(payload), encoding='utf-8')
        table = self.db['pairs']
        huge = 2**60 + 3
        table.insert_many([{'id': huge, 'group': 1, 'value': 7}, {'id': huge, 'group': 2, 'value': 8}])
        self.assertEqual(table.kv.get([huge, 2])['value'], 8)
        table.kv.update_many({(huge, 1): {'value': 99}})
        self.assertEqual(table.kv.get_many([(huge, 1)])[(huge, 1)]['value'], 99)
        with self.assertRaises(ValueError):
            table.kv.update((huge, 1), {'group': 3})

    def test_threaded_patches_and_consistent_pipeline_snapshot(self):
        self.seed(2)
        def write(field):
            for n in range(20):
                self.table.update_by_pk(0, {field: n})
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(write, ('group', 'value')))
        self.assertEqual(self.table.kv.get(0)['group'], 19)
        self.assertEqual(self.table.kv.get(0)['value'], 19)
        self.table.kv.update_many({0: {'value': 0}, 1: {'value': 0}})
        def writer():
            for n in range(30):
                self.table.kv.update_many({0: {'value': n}, 1: {'value': n}})
        with ThreadPoolExecutor(1) as pool:
            task = pool.submit(writer)
            for _ in range(60):
                rows = self.table.read_pipeline().get(0).get(1).execute()
                self.assertEqual(rows[0]['value'], rows[1]['value'])
            task.result()

    def test_multiprocess_append_visible_without_reopen(self):
        self.seed(1)
        self.assertEqual(self.table.kv.get(0), _row(0))
        ctx = mp.get_context('spawn')
        with ctx.Pool(4) as pool:
            pool.starmap(append_worker, [(str(self.root), 100 + i * 20) for i in range(4)])
        self.assertEqual(len(self.table), 81)
        self.assertEqual(self.table.kv.get(179), _row(179))

    def test_dedicated_writer_batches_spawn_workers_and_hot_sync(self):
        self.seed(1)
        ctx = mp.get_context('spawn')
        writer = SQLiteWriteProcess(self.root, schema_directory=self.root.parent / 'def/table',
                                     batch_size=64, flush_interval=0.01,
                                     start_method='spawn')
        writer.start()
        clients = [writer.client() for _ in range(4)]
        workers = [ctx.Process(target=dedicated_append_worker,
                               args=(client, 100 + n * 25))
                   for n, client in enumerate(clients)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
                self.assertEqual(worker.exitcode, 0)
            writer.flush()
            self.assertEqual(len(self.table), 101)
            self.assertEqual(self.table.kv.get(199), _row(199))
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.kill(); worker.join(10)
            writer.close()

    def test_busy_timeout_and_recovery(self):
        self.seed(2)
        with SQLiteDatabase(self.root, busy_timeout=0.02, transaction_retries=1) as other:
            blocked = other['records']
            c = self.table._connection()
            c.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaises(SQLiteBusyError):
                    blocked.update_by_pk(0, {'value': 77})
            finally:
                c.rollback()
            blocked.update_by_pk(0, {'value': 88})
        self.assertEqual(self.table.kv.get(0)['value'], 88)

    def test_forced_process_exit_preserves_commits_discards_uncommitted_write(self):
        self.seed(3)
        ctx = mp.get_context('spawn')
        parent, child = ctx.Pipe()
        worker = ctx.Process(target=crash_writer, args=(str(self.root), child))
        worker.start()
        child.close()
        try:
            self.assertTrue(parent.poll(20), 'child did not acknowledge writes')
            self.assertTrue(parent.recv())
            worker.kill()
            worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.table.kv.get(0)['value'], 999)
            self.assertIsNone(self.table.kv.get(1))
            self.assertEqual(self.table.kv.get(1000), _row(1000))
            self.assertEqual(self.table._connection().execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            if worker.is_alive():
                worker.kill(); worker.join(10)
            parent.close()


if __name__ == '__main__':
    unittest.main()
