"""File-mapped direct writers vs one WAL group-commit process.

python benchmarks/benchmark_file_mapped_writer.py --rows 50000 --repeat 3 --output output/file-mapped-writer-50000.json
Timing waits for every durable acknowledgement. Initial data, process startup
and connection warmup are excluded from workload time and reported separately.
Only isolated TemporaryDirectory databases are created or changed.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import multiprocessing as mp
import platform
import statistics
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.benchmark_redis_vs_file import _definition, _row
from core import FileMappedDatabase, FileMappedWriteProcess


SCENARIOS = ('process_append', 'process_update', 'process_kv_update', 'process_equal_update', 'process_delete',
             'process_batch_append', 'process_batch_update',
             'thread_mixed', 'single_append')


def _worker(root, client, scenario, worker, count, rows, ready, go, results, shared_table=None):
    db = None
    try:
        if shared_table is not None:
            table = shared_table
        elif client is None or scenario == 'thread_mixed':
            db = FileMappedDatabase(root)
            table = db['records']
        if client is not None:
            client.connect()
        keys = list(range(worker * count, (worker + 1) * count))
        ready.put(True)
        if not go.wait(60):
            raise TimeoutError('start barrier timed out')
        if 'append' in scenario:
            data = [_row(rows + key) for key in keys]
            if 'batch' in scenario:
                changed = (table.insert_many(data) if client is None
                           else client.insert_many('records', data))
                assert changed == count
            else:
                for row in data:
                    if client is None:
                        assert table.insert_one(row) == 1
                    else:
                        assert client.append('records', row) == 1
        elif scenario == 'process_batch_update':
            updates = {k: {'value': -k - 1} for k in keys}
            changed = (table.kv.update_many(updates) if client is None
                       else client.update_many('records', updates))
            assert changed == count
        else:
            for key in keys:
                if scenario == 'thread_mixed':
                    assert table.get_by_pk(key)['value'] == key * 2
                if scenario == 'process_kv_update':
                    changed = (table.kv.update(key, {'value': -key - 1}) if client is None
                               else client.update_by_key('records', key, {'value': -key - 1}))
                elif scenario == 'process_equal_update':
                    changed = (table.update({'value': -key - 1}, {'name': f'row-{key}'}) if client is None
                               else client.update('records', {'value': -key - 1}, {'name': f'row-{key}'}))
                elif scenario == 'process_delete':
                    changed = (table.delete_by_pk(key) if client is None
                               else client.delete_by_pk('records', key))
                else:
                    changed = (table.update_by_pk(key, {'value': -key - 1}) if client is None
                               else client.update_by_pk('records', key, {'value': -key - 1}))
                assert changed == 1
                if scenario == 'thread_mixed':
                    assert table.get_by_pk(key)['value'] == -key - 1
        results.put({'ok': True})
    except BaseException:
        # Also signal ready if setup failed so the parent never hangs at its barrier.
        ready.put(False)
        results.put({'ok': False, 'error': traceback.format_exc()})
    finally:
        if db is not None:
            db.close()
        if client is not None:
            client.close()


def _run(root, mode, scenario, args):
    workers = 8 if scenario == 'thread_mixed' else 1 if scenario == 'single_append' else args.processes
    count = min(args.writes, 100) if scenario == 'single_append' else args.writes
    _definition(root)
    with FileMappedDatabase(root) as db:
        table = db['records']
        table.insert_many(_row(i) for i in range(args.rows))
        # A reader that stays open throughout verifies hot visibility too.
        assert table.get_by_pk(0) == _row(0)
        config = dict(engine='fixed mmap', durability='WAL fsync before ACK', record_size=table._fixed_store.record_size)
        ctx = mp.get_context('spawn')
        ready, results, go = ctx.Queue(), ctx.Queue(), ctx.Event()
        writer, pool, children, futures = None, None, [], []
        start_setup = time.perf_counter()
        try:
            if mode == 'central':
                writer = FileMappedWriteProcess(root, batch_size=args.batch_size,
                                            flush_interval=args.flush_interval).start()
            client = writer.client() if writer else None
            if scenario == 'thread_mixed':
                pool = ThreadPoolExecutor(workers)
            for worker in range(workers):
                task_args = (str(root), client, scenario, worker, count, args.rows, ready, go, results)
                if pool:
                    futures.append(pool.submit(_worker, *task_args, table))
                else:
                    process = ctx.Process(target=_worker, args=task_args)
                    process.start()
                    children.append(process)
            for _ in range(workers):
                if not ready.get(timeout=60):
                    raise RuntimeError(results.get(timeout=5))
            setup_seconds = time.perf_counter() - start_setup
            start = time.perf_counter()
            go.set()
            for _ in range(workers):
                result = results.get(timeout=60)
                if not result['ok']:
                    raise RuntimeError(result['error'])
            seconds = time.perf_counter() - start
            teardown_start = time.perf_counter()
            for process in children:
                process.join(10)
                assert process.exitcode == 0
            for future in futures:
                future.result(5)
            stats = writer.stats() if writer else None
            if writer:
                writer.close()
                writer = None
            teardown_seconds = time.perf_counter() - teardown_start
        finally:
            go.set()
            for process in children:
                if process.is_alive():
                    process.kill()
                    process.join(5)
            if pool:
                pool.shutdown()
            if writer:
                writer.close()
            ready.close()
            results.close()
        expected = {i: _row(i) for i in range(args.rows)}
        for i in range(workers * count):
            if 'append' in scenario:
                expected[args.rows + i] = _row(args.rows + i)
            elif scenario == 'process_delete':
                del expected[i]
            else:
                expected[i]['value'] = -i - 1
        actual = table.select(order_by='id')
        assert actual == [expected[k] for k in sorted(expected)]
        assert table.count() == len(expected)
        with FileMappedDatabase(root) as reopened:
            assert reopened['records'].select(order_by='id') == actual
        digest = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return dict(seconds=seconds, rows_per_second=workers * count / seconds,
                    setup_seconds=setup_seconds, teardown_seconds=teardown_seconds,
                    workers=workers, writes_per_worker=count, stats=stats,
                    storage_config=config, final_rows=len(actual), sha256=digest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=50000)
    parser.add_argument('--writes', type=int, default=50)
    parser.add_argument('--processes', type=int, default=4)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--flush-interval', type=float, default=0.001)
    parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS, default=SCENARIOS)
    parser.add_argument('--output', type=Path, default=Path('output/file-mapped-writer-50000.json'))
    args = parser.parse_args()
    if args.rows < max(args.processes, 8) * args.writes or min(args.repeat, args.writes, args.processes) < 1:
        parser.error('rows must cover each worker key range; counts must be positive')
    report = dict(timestamp_utc=datetime.now(timezone.utc).isoformat(),
                  environment=dict(platform=platform.platform(), python=sys.version,
                                   executable=sys.executable),
                  parameters={**vars(args), 'output': str(args.output)}, runs={}, comparison={})
    with tempfile.TemporaryDirectory(prefix='rtdb-file-mapped-writer-bench-') as folder:
        for scenario in args.scenarios:
            runs = report['runs'][scenario] = {'direct': [], 'central': []}
            for repeat in range(args.repeat):
                # Alternate order to reduce a systematic cache/load bias.
                for mode in (('direct', 'central') if repeat % 2 == 0 else ('central', 'direct')):
                    root = Path(folder) / f'{scenario}-{repeat}-{mode}' / 'dat'
                    result = _run(root, mode, scenario, args)
                    runs[mode].append(result)
                    print(f'{scenario:24} {mode:7} round={repeat + 1} {result["seconds"]:.6f}s', flush=True)
            assert len({r['sha256'] for samples in runs.values() for r in samples}) == 1
            direct = statistics.median(r['seconds'] for r in runs['direct'])
            central = statistics.median(r['seconds'] for r in runs['central'])
            report['comparison'][scenario] = dict(direct_seconds=direct, central_seconds=central,
                speedup=direct / central,
                central_commits=[r['stats']['commits'] for r in runs['central']],
                final_rows=runs['direct'][0]['final_rows'], sha256=runs['direct'][0]['sha256'])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['comparison'], indent=2))


if __name__ == '__main__':
    mp.freeze_support()
    main()
