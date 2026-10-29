"""Real IPC, group-commit isolation and failure tests for the FileMapped writer."""
import json
import multiprocessing as mp
import random
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from benchmarks.benchmark_redis_vs_file import _definition, _row
from core import FileMappedDatabase, FileMappedWriteProcess, FileMappedWriteProcessError, StorageBusyError
from core.table_schema import SchemaError
from core.file_mapped_db import _FileMappedWriterService, _FixedRecordStore, _locked


def shared_endpoint_worker(client, start):
    # The SAME endpoint may be passed to every process: reply connections are
    # local, so one consumer cannot steal another consumer's acknowledgement.
    client.connect()
    for i in range(start, start + 20):
        assert client.append('records', _row(i)) == 1
    client.close()


class FileMappedWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rtdb-file_mapped-writer-')
        self.root = Path(self.temp.name) / 'dat'
        _definition(self.root)
        self.db = FileMappedDatabase(self.root)
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
            with self.assertRaises(SchemaError):
                writer.append('records', _row(2 ** 80))
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
            self.assertEqual(writer.update_by_pk('pairs', (1, 1), {'group': 5}), 1)
            self.assertEqual(pairs.get_by_pk((1, 5))['value'], 2)

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

    def test_busy_rejection_and_timeout_without_replay(self):
        with self.db.write_process(lock_timeout=0.02) as writer:
            writer.append('records', _row(0))
            with _locked(self.table._fixed_store.lock_path):
                with self.assertRaises(StorageBusyError):
                    writer.append('records', _row(1))
            writer.append('records', _row(2))
            self.assertIsNone(self.table.kv.get(1))
        with self.db.write_process(request_timeout=0.1, lock_timeout=2) as writer:
            with _locked(self.table._fixed_store.lock_path):
                with self.assertRaisesRegex(FileMappedWriteProcessError, 'outcome unknown'):
                    writer.append('records', _row(3))
            writer.flush()
            self.assertEqual(self.table.get_by_pk(3), _row(3))
            self.assertEqual(writer.stats()['requests'], 1)

    def test_bounded_queue_rejects_unaccepted_work(self):
        with self.db.write_process(queue_size=1, batch_size=1) as writer:
            writer.append('records', _row(0))
            client = writer.client()
            with ThreadPoolExecutor(2) as pool:
                with _locked(self.table._fixed_store.lock_path):
                    first = pool.submit(client.append, 'records', _row(1))
                    self.wait_for(lambda: writer.stats()['active_requests'] == 1)
                    second = pool.submit(client.append, 'records', _row(2))
                    self.wait_for(lambda: writer.stats()['queued_requests'] == 1)
                    with self.assertRaisesRegex(StorageBusyError, 'not accepted'):
                        client.append('records', _row(3))
                self.assertEqual(first.result(5), 1)
                self.assertEqual(second.result(5), 1)
            self.assertIsNone(self.table.kv.get(3))

    def test_killed_writer_keeps_acknowledged_commits(self):
        writer = self.db.write_process().start()
        writer.insert_many('records', [_row(0), _row(1)])
        writer.update_by_pk('records', 0, {'value': 888})
        writer.delete_by_pk('records', 1)
        client = writer.client()
        with ThreadPoolExecutor(1) as pool:
            with _locked(self.table._fixed_store.lock_path):
                pending = pool.submit(client.append, 'records', _row(2))
                self.wait_for(lambda: writer.stats()['active_requests'] == 1)
                writer._owner._process.kill()
                writer._owner._process.join(5)
                with self.assertRaisesRegex(FileMappedWriteProcessError, 'outcome unknown'):
                    pending.result(5)
        with self.assertRaises(FileMappedWriteProcessError):
            writer.close()
        self.db.close()
        with FileMappedDatabase(self.root) as recovered:
            self.assertEqual(recovered['records'].all(), [{**_row(0), 'value': 888}])

    def test_lifecycle_configuration_and_unknown_table(self):
        for options in ({'queue_size': 0}, {'batch_size': 1.5}, {'flush_interval': float('nan')},
                        {'request_timeout': float('inf')}, {'lock_timeout': 0}):
            with self.assertRaises(ValueError):
                FileMappedWriteProcess(self.root, **options)
        writer = self.db.write_process()
        self.assertIsNone(writer.pid)
        writer.close()
        writer.close()
        with self.assertRaises(FileMappedWriteProcessError):
            writer.start()
        with self.db.write_process() as running:
            with self.assertRaises(SchemaError):
                running.append('unknown_table', _row(0))
            self.assertFalse((self.root / 'unknown_table.table').exists())
            running.append('records', _row(1))
            with self.assertRaises(ValueError):
                running.delete('records')
        with self.assertRaises(FileMappedWriteProcessError):
            running.append('records', _row(2))

    def test_wal_failures_never_acknowledged_or_replayed(self):
        for stage in ('before_wal', 'after_wal'):
            with self.subTest(stage=stage):
                service = _FileMappedWriterService(dict(directory=str(self.root)), 8, 0.05, 16, 5)
                self.assertNotIn('error', service.status())
                target = '_append_wal_locked' if stage == 'before_wal' else '_apply_entries_and_header_locked'
                with patch.object(_FixedRecordStore, target, side_effect=OSError('injected disk error')):
                    with ThreadPoolExecutor(2) as pool:
                        results = list(pool.map(lambda n: service.execute('records', 'insert', [_row(n)], str(n)), [1, 2]))
                    self.assertTrue(all(r.get('unknown') and 'result' not in r for r in results))
                    self.assertIn('error', service.stop())
                # Before WAL there is nothing to recover. After WAL, both
                # writes recover, even though neither received a success ACK.
                self.db.close()
                with FileMappedDatabase(self.root) as db:
                    self.assertEqual(db['records'].select(order_by='id'), [] if stage == 'before_wal' else [_row(1), _row(2)])

    def test_no_ack_before_wal_sync_and_publish(self):
        service = _FileMappedWriterService(dict(directory=str(self.root)), 8, 0, 16, 5)
        self.assertNotIn('error', service.status())
        entered, release = threading.Event(), threading.Event()
        original = _FixedRecordStore._append_wal_locked
        def delayed(store, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test gate')
            return original(store, *args, **kwargs)
        try:
            with patch.object(_FixedRecordStore, '_append_wal_locked', delayed):
                with ThreadPoolExecutor(1) as pool:
                    response = pool.submit(service.execute, 'records', 'insert', [_row(1)], 'one')
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertFalse(response.done())
                    finally:
                        release.set()
                    self.assertEqual(response.result(5), {'result': 1})
        finally:
            service.stop()
        self.assertEqual(self.table.get_by_pk(1), _row(1))

    def test_ordered_staging_rollback_key_change_and_external_commit(self):
        # Invoke a deterministic group to verify request order independently
        # of nondeterministic arrival order among real RPC threads.
        self.table.insert_many(_row(i) for i in range(4))
        service = object.__new__(_FileMappedWriterService)
        service._stats = dict(active_requests=0, requests=0, commits=0, max_group=0, succeeded=0, failed=0)
        operations = [
            ('update_pk', (0, {'id': 10, 'group': 90})),
            ('update', ({'value': 100}, {'group': 90}, None)),
            ('update_many_pk', [(1, {'value': 999}), (999, {'value': 1})]),
            ('delete_pk', 10),
            ('insert', [_row(10)]),
            ('update_pk', (10, {'value': 101})),
            ('delete', ({'group': -1}, None)),
            ('update_pk', (999, {'value': 1})),
        ]
        group = [dict(table='records', operation=op, payload=data, done=threading.Event()) for op, data in operations]
        service._commit(self.db, group)
        self.assertEqual([r['response'].get('result') for r in group], [1, 1, None, 1, 1, 1, 0, None])
        self.assertEqual(service._stats['commits'], 1)
        self.assertEqual(self.table.all(), [_row(1), _row(2), _row(3), {**_row(10), 'value': 101}])
        with FileMappedDatabase(self.root) as external:
            external['records'].update_by_pk(1, {'id': 20})
        group = [dict(table='records', operation='update_pk', payload=(20, {'value': 55}), done=threading.Event())]
        service._commit(self.db, group)
        self.assertEqual(group[0]['response'], {'result': 1})
        self.assertEqual(self.table.get_by_pk(20)['value'], 55)

    def test_duplicate_secondary_values_preserved_in_incremental_indexes(self):
        self.table.insert_many(_row(i) for i in range(40))
        self.table.select(where={'group': 0})
        store = self.table._fixed_store
        path = store._index_path(('group',))
        before = path.read_bytes()
        with self.db.write_process() as writer:
            writer.insert_many('records', [{**_row(i), 'group': 0} for i in (100, 101, 102)])
        after = path.read_bytes()
        self.assertGreater(len(after), len(before))
        # The descriptor is updated; the base payload remains byte-identical.
        before_header = store._decode_index_header_slot(before[:128])
        before_start = 256 + before_header['metadata_length']
        after_header = max(filter(None, [store._decode_index_header_slot(after[:128]),
                                         store._decode_index_header_slot(after[128:256])]),
                           key=lambda item: item['generation'])
        after_start = 256 + after_header['metadata_length']
        self.assertEqual(after[after_start:after_start + before_header['payload_length']],
                         before[before_start:before_start + before_header['payload_length']])
        expected = [0, 16, 32, 100, 101, 102]
        self.assertEqual([r['id'] for r in self.table.select(where={'group': 0})], expected)
        with FileMappedDatabase(self.root) as reopened:
            with patch.object(reopened['records']._fixed_store, '_build_key_index_from_map', side_effect=AssertionError('unnecessary rebuild')):
                self.assertEqual([r['id'] for r in reopened['records'].select(where={'group': 0})], expected)
        # Cover the other delta path: additions to an already loaded local
        # secondary cache must preserve repeated secondary keys as well.
        self.table.insert_many([{**_row(i), 'group': 0} for i in (103, 104)])
        expected += [103, 104]
        self.assertEqual([r['id'] for r in self.table.select(where={'group': 0})], expected)
        with FileMappedDatabase(self.root) as reopened:
            with patch.object(reopened['records']._fixed_store, '_build_key_index_from_map', side_effect=AssertionError('unnecessary rebuild')):
                self.assertEqual([r['id'] for r in reopened['records'].select(where={'group': 0})], expected)

    def wait_for(self, condition):
        deadline = time.perf_counter() + 5
        while not condition():
            if time.perf_counter() >= deadline:
                self.fail('timed out waiting for writer state')
            time.sleep(0.005)

    def test_random_mixed_groups_match_direct_crud(self):
        other_root = self.root.parent / 'reference' / 'dat'
        _definition(other_root)
        rng = random.Random(37571)
        service = object.__new__(_FileMappedWriterService)
        service._stats = dict(active_requests=0, requests=0, commits=0, max_group=0, succeeded=0, failed=0)
        with FileMappedDatabase(other_root) as reference:
            target = reference['records']
            target.insert_many(_row(i) for i in range(20))
            self.table.insert_many(_row(i) for i in range(20))
            for _ in range(30):
                requests, expected = [], []
                for _ in range(10):
                    key = rng.randrange(40)
                    kind = rng.choice(('insert', 'update_pk', 'delete_pk', 'update', 'delete'))
                    values = {'value': rng.randrange(1000)}
                    if rng.randrange(5) == 0:
                        values['group'] = rng.randrange(16)
                    payload = ([_row(key)] if kind == 'insert' else (key, values) if kind == 'update_pk'
                               else key if kind == 'delete_pk' else (values, {'group': key % 16}, 2)
                               if kind == 'update' else ({'group': key % 16}, 1))
                    requests.append(dict(table='records', operation=kind, payload=payload, done=threading.Event()))
                    try:
                        if kind == 'insert':
                            result = target.insert_many(payload)
                        elif kind == 'update_pk':
                            result = target.update_by_pk(*payload)
                        elif kind == 'delete_pk':
                            result = target.delete_by_pk(payload)
                        elif kind == 'update':
                            result = target.update(payload[0], payload[1], limit=payload[2])
                        else:
                            result = target.delete(payload[0], limit=payload[1])
                        expected.append(result)
                    except (SchemaError, KeyError) as exc:
                        expected.append(type(exc).__name__)
                service._commit(self.db, requests)
                self.assertEqual([r['response'].get('error', r['response'].get('result')) for r in requests], expected)
                self.assertEqual(self.table.all(), target.all())
                for group in range(16):
                    self.assertEqual(self.table.select({'group': group}), target.select({'group': group}))

    def test_reused_slots_preserve_order_and_empty_requests_do_not_commit(self):
        path = self.root.parent / 'def/table/records.json'
        definition = json.loads(path.read_text())
        definition['head'].update(eng_name='tiny')
        (path.parent / 'tiny.json').write_text(json.dumps(definition))
        table = self.db['tiny']
        table.insert_many([_row(0), _row(1)])
        service = object.__new__(_FileMappedWriterService)
        service._stats = dict(active_requests=0, requests=0, commits=0, max_group=0, succeeded=0, failed=0)
        operations = []
        for _ in range(8):
            operations.extend([('delete_pk', 0), ('insert', [_row(0)])])
        operations.append(('update', ({'value': 123}, {'group': 0}, 1)))
        requests = [dict(table='tiny', operation=op, payload=p, done=threading.Event()) for op, p in operations]
        service._commit(self.db, requests)
        self.assertTrue(all(r['response'] == {'result': 1} for r in requests))
        self.assertEqual(table.all(), [_row(1), {**_row(0), 'value': 123}])
        table.delete_by_pk(1)
        table.append(_row(2))
        self.assertEqual([r['id'] for r in table.all()], [0, 2])
        before = service._stats['commits']
        requests = [dict(table='tiny', operation=op, payload=p, done=threading.Event()) for op, p in
                    [('insert', []), ('delete', ({'id': 999}, None)), ('update_pk', (0, {'value': 123}))]]
        service._commit(self.db, requests)
        self.assertEqual([r['response']['result'] for r in requests], [0, 0, 1])
        self.assertEqual(service._stats['commits'], before)

    def test_float_keys_use_encoded_value_and_reject_nonfinite_values(self):
        path = self.root.parent / 'def/table/records.json'
        definition = json.loads(path.read_text())
        definition['head']['eng_name'] = 'floating'
        definition['columns'][0]['data_type'] = 'float'
        (path.parent / 'floating.json').write_text(json.dumps(definition))
        with self.db.write_process() as writer:
            writer.append('floating', _row(2 ** 53))
            with self.assertRaises(SchemaError):
                writer.append('floating', _row(2 ** 53 + 1))
            for value in (float('inf'), float('nan')):
                with self.assertRaises(SchemaError):
                    writer.append('floating', {**_row(1), 'id': value})
            self.assertEqual(writer.update_by_pk('floating', 2 ** 53, {'value': 17}), 1)
        self.assertEqual(self.db['floating'].get_by_pk(2 ** 53)['value'], 17)


if __name__ == '__main__':
    unittest.main()
