"""Real IPC, group-commit isolation and failure tests for the SQLite writer."""
import json
import multiprocessing as mp
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from benchmarks.benchmark_redis_vs_file import _definition, _row
from core import SQLiteDatabase, SQLiteWriteProcess, SQLiteWriteProcessError, SQLiteBusyError
from core.table_schema import SchemaError
from core.sqlite_db import _SQLiteWriterService, SQLiteTable


def shared_endpoint_worker(client, start):
    # The SAME endpoint may be passed to every process: reply connections are
    # local, so one consumer cannot steal another consumer's acknowledgement.
    client.connect()
    for i in range(start, start + 20):
        assert client.append('records', _row(i)) == 1
    client.close()


class SQLiteWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rtdb-sqlite-writer-')
        self.root = Path(self.temp.name) / 'dat'
        _definition(self.root)
        self.db = SQLiteDatabase(self.root)
        self.table = self.db['records']

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_spawn_workers_share_endpoint_and_existing_reader(self):
        with self.db.write_process() as writer:
            client = writer.client()
            client.connect()  # even an already-used endpoint must spawn safely
            ctx = mp.get_context('spawn')
            with ctx.Pool(4) as pool:
                pool.starmap(shared_endpoint_worker, [(client, i * 20) for i in range(4)])
            self.assertEqual(len(self.table), 80)
            self.assertEqual(self.table.get_by_pk(79), _row(79))
            stats = writer.stats()
            self.assertEqual(stats['succeeded'], 80)
            self.assertLess(stats['commits'], 80)
            self.assertGreater(stats['max_group'], 1)
        self.assertEqual(self.table.storage_config()['synchronous'], 2)

    def test_mixed_group_bad_request_does_not_rollback_good_requests(self):
        self.table.insert_many(_row(i) for i in range(4))
        with self.db.write_process(flush_interval=0.1) as writer:
            client = writer.client()
            barrier = threading.Barrier(4)
            calls = [lambda: client.insert_many('records', [_row(100), _row(0)]),
                     lambda: client.append('records', _row(101)),
                     lambda: client.update_by_pk('records', 1, {'value': 700}),
                     lambda: client.delete_by_pk('records', 2)]
            def run(i):
                client.connect()
                barrier.wait(10)
                try:
                    return calls[i]()
                except SchemaError:
                    return 'constraint'
            with ThreadPoolExecutor(4) as pool:
                results = list(pool.map(run, range(4)))
            self.assertEqual(results, ['constraint', 1, 1, 1])
            self.assertIsNone(self.table.kv.get(100))
            self.assertEqual(self.table.get_by_pk(101), _row(101))
            self.assertEqual(self.table.get_by_pk(1)['value'], 700)
            self.assertIsNone(self.table.kv.get(2))
            self.assertEqual(writer.stats()['commits'], 1)
            self.assertEqual(writer.stats()['failed'], 1)

    def test_request_atomicity_key_batches_and_condition_limits(self):
        self.table.insert_many(_row(i) for i in range(20))
        with self.db.write_process(flush_interval=0) as writer:
            before = self.table.all()
            with self.assertRaises(KeyError):
                writer.update_many('records', [(0, {'value': -1}), (1000, {'value': 9})])
            with self.assertRaises(KeyError):
                writer.delete_many('records', [0, 1000])
            self.assertEqual(self.table.all(), before)
            self.assertEqual(writer.update_many('records', {0: {'value': 8}, 1: {'value': 9}}), 2)
            self.assertEqual(writer.update('records', {'value': 88}, {'group': 1}, limit=1), 1)
            self.assertEqual(self.table.get_by_pk(17)['value'], 34)
            self.assertEqual(writer.update_by_key('records', 'row-2', {'value': 99}, 'name'), 1)
            self.assertEqual(writer.delete_by_key('records', 'row-2', 'name'), 1)
            self.assertEqual(writer.delete('records', {'group': 1}, limit=1), 1)
            with self.assertRaises(ValueError):
                writer.update('records', {'value': 1}, 'id > 5')
            self.assertEqual(writer.append('records', {**_row(1000), 'name': 'x' * 333}), 1)
            with self.assertRaises(ValueError):
                writer.update_many('records', [(0, {'value': 1}), (0, {'value': 2})])
            self.assertTrue(writer.flush())

    def test_composite_keys_capacity_and_separate_tables(self):
        path = self.root.parent / 'def/table/records.json'
        definition = json.loads(path.read_text())
        definition['head'].update(eng_name='pairs')
        definition['columns'][1]['primary_key'] = True
        (path.parent / 'pairs.json').write_text(json.dumps(definition))
        pairs = self.db['pairs']
        with self.db.write_process() as writer:
            writer.insert_many('pairs', [{**_row(1), 'group': n} for n in range(2)])
            writer.append('records', _row(1))
            writer.update_by_pk('pairs', (1, 0), {'value': 99})
            self.assertEqual(pairs.get_by_pk((1, 0))['value'], 99)
            self.assertEqual(self.table.get_by_pk(1)['value'], 2)
            self.assertEqual(writer.append('pairs', {**_row(2), 'group': 0}), 1)
            self.assertEqual(len(pairs), 3)
            writer.delete_by_pk('pairs', (1, 0))
            with self.assertRaises(ValueError):
                writer.update_by_pk('pairs', (1, 1), {'group': 5})

    def test_threads_patch_without_lost_fields_and_reads_stay_direct(self):
        self.table.append(_row(1))
        with self.db.write_process() as writer:
            client = writer.client()
            def write(field):
                for n in range(20):
                    client.update_by_pk('records', 1, {field: n})
                    self.assertIn(field, self.table.get_by_pk(1))
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(write, ['value', 'group']))
        self.assertEqual(self.table.get_by_pk(1)['value'], 19)
        self.assertEqual(self.table.get_by_pk(1)['group'], 19)

    def test_busy_failure_not_replayed_and_service_recovers(self):
        with self.db.write_process(busy_timeout=0.02, transaction_retries=1) as writer:
            writer.append('records', _row(0))
            c = self.table._connection()
            c.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaises(SQLiteBusyError):
                    writer.append('records', _row(1))
            finally:
                c.rollback()
            writer.append('records', _row(2))
            self.assertIsNone(self.table.kv.get(1))

    def test_timeout_reports_unknown_and_does_not_replay(self):
        with self.db.write_process(request_timeout=0.1, busy_timeout=2) as writer:
            writer.append('records', _row(0))
            c = self.table._connection()
            c.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaisesRegex(SQLiteWriteProcessError, 'outcome unknown'):
                    writer.append('records', _row(1))
            finally:
                c.rollback()
            writer.flush()
            self.assertEqual(self.table.get_by_pk(1), _row(1))
            self.assertEqual(writer.stats()['requests'], 2)

    def test_bounded_queue_rejects_unaccepted_work(self):
        with self.db.write_process(queue_size=1, batch_size=1, busy_timeout=3) as writer:
            writer.append('records', _row(0))
            client = writer.client()
            c = self.table._connection()
            c.execute('BEGIN IMMEDIATE')
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(client.append, 'records', _row(1))
                try:
                    self.wait_for(lambda: writer.stats()['active_requests'] == 1)
                    second = pool.submit(client.append, 'records', _row(2))
                    self.wait_for(lambda: writer.stats()['queued_requests'] == 1)
                    with self.assertRaisesRegex(SQLiteBusyError, 'not accepted'):
                        client.append('records', _row(3))
                finally:
                    c.rollback()
                self.assertEqual(first.result(5), 1)
                self.assertEqual(second.result(5), 1)
            self.assertIsNone(self.table.kv.get(3))

    def test_killed_writer_keeps_acknowledged_commits(self):
        writer = self.db.write_process(busy_timeout=10).start()
        writer.insert_many('records', [_row(0), _row(1)])
        writer.update_by_pk('records', 0, {'value': 888})
        writer.delete_by_pk('records', 1)
        client = writer.client()
        c = self.table._connection()
        c.execute('BEGIN IMMEDIATE')
        try:
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(client.append, 'records', _row(2))
                self.wait_for(lambda: writer.stats()['active_requests'] == 1)
                writer._owner._process.kill()
                writer._owner._process.join(5)
                with self.assertRaisesRegex(SQLiteWriteProcessError, 'outcome unknown'):
                    pending.result(5)
        finally:
            c.rollback()
            with self.assertRaises(SQLiteWriteProcessError):
                writer.close()
        self.assertEqual(self.table.get_by_pk(0)['value'], 888)
        self.assertIsNone(self.table.kv.get(1))
        self.assertIsNone(self.table.kv.get(2))
        self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_lifecycle_validation(self):
        with self.assertRaises(ValueError):
            SQLiteWriteProcess(self.root, queue_size=0)
        writer = self.db.write_process()
        self.assertIsNone(writer.pid)
        writer.close()
        writer.close()
        with self.assertRaises(SQLiteWriteProcessError):
            writer.start()
        with self.db.write_process() as running:
            running.append('records', _row(1))
        with self.assertRaises(SQLiteWriteProcessError):
            running.append('records', _row(2))

    def test_commit_error_never_acknowledges_or_replays_success(self):
        # Fault injection at the actual SQLite COMMIT: deny the SQL transaction
        # statement after the writes have executed. The ordinary _write path
        # must roll back and no request can receive a success envelope.
        service = _SQLiteWriterService(dict(directory=str(self.root)), 8, 0.1, 16, 5)
        self.assertNotIn('error', service.status())
        self.assertEqual(service.execute('records', 'insert', [_row(0)], 'seed'), {'result': 1})
        original = SQLiteTable._connection
        def deny_commit(table):
            connection = original(table)
            connection.set_authorizer(lambda action, arg1, *_args:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT'
                else sqlite3.SQLITE_OK)
            return connection
        try:
            with patch.object(SQLiteTable, '_connection', deny_commit):
                with ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(lambda n: service.execute('records', 'insert', [_row(n)], str(n)), [1, 2]))
            self.assertTrue(all(r.get('unknown') for r in results))
            self.assertTrue(all('result' not in r for r in results))
            self.assertEqual(service.status()['result']['commits'], 1)
        finally:
            self.assertNotIn('error', service.stop())
        self.assertEqual(self.table.all(), [_row(0)])

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() >= deadline:
                self.fail('timed out waiting for writer state')
            time.sleep(0.005)


if __name__ == '__main__':
    unittest.main()
