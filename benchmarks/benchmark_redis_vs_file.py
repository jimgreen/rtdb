"""Validated 50,000-row file-mapped / Redis AOF comparison.

python benchmarks/benchmark_redis_vs_file.py --redis-url redis://127.0.0.1:6390/0 --repeat 3 --output output/redis-comparison.json
Only this run's random namespace is cleaned; never FLUSHDB or CONFIG SET.
Redis connection/configuration failures are distinct from workload bugs, which
fail the run. Ratios are median(file seconds) / median(Redis seconds).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import multiprocessing as mp
import platform
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import FileMappedDatabase, RedisDatabase, SQLiteDatabase, TableDefinition


def _definition(root):
    directory = root.parent / 'def' / 'table'
    fields = [('id', True, 'int', 8), ('group', False, 'int', 8),
              ('value', False, 'int', 8), ('name', False, 'str', 32)]
    definition = TableDefinition.from_mapping({
        'head': {'chn_name': '基准记录', 'eng_name': 'records'},
        'columns': [{'chn_name': f, 'eng_name': f, 'primary_key': pk,
                     'data_type': typ, 'disp_type': 'text', 'disp_menu': ''}
                    for f,pk,typ,length in fields],
    }, directory, 'records')
    definition.to_json(directory)
    return directory


def _row(i):
    return {'id': i, 'group': i % 16, 'value': i * 2, 'name': f'row-{i}'}


def _db(kind, root, url, durability):
    if kind == 'redis':
        return RedisDatabase(root, redis_url=url, durability=durability)
    if kind == 'sqlite':
        return SQLiteDatabase(root)
    if kind == 'file_mapped':
        return FileMappedDatabase(root)
    raise ValueError(f'unknown backend: {kind}')


def _process_worker(args):
    kind, root, url, durability, start, batched = args
    with _db(kind, root, url, durability) as db:
        table = db['records']
        rows = [_row(i) for i in range(start, start + 25)]
        if batched:
            assert table.insert_many(rows) == 25
        else:
            for row in rows:
                table.append(row)


def _processes(kind, root, url, durability, start, batched):
    args = [(kind, str(root), url, durability, start + worker * 25, batched) for worker in range(4)]
    with mp.get_context('spawn').Pool(4) as pool:
        pool.map(_process_worker, args)


def _threads(table, rows):
    def work(worker):
        for n in range(60):
            assert table.kv.get(rows // 2)['id'] == rows // 2
            if n % 12 == 0:
                table.append(_row(rows + 10000 + worker * 5 + n // 12))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(work, range(8)))


def _cleanup(db):
    # Exclusive random path-derived namespace; bounded DEL avoids command limits.
    keys = list(db.redis.scan_iter(match=db.namespace + ':*', count=1000))
    for i in range(0, len(keys), 1000):
        db.redis.delete(*keys[i:i+1000])


def _run_backend(kind, root, rows, url, durability, *, extended=False):
    db = _db(kind, root, url, durability)
    table = db['records']
    expected = {i: _row(i) for i in range(rows)}
    seconds = {}
    def timed(name, work):
        start = time.perf_counter()
        result = work()
        seconds[name] = time.perf_counter() - start
        return result
    try:
        def seed():
            for start in range(0, rows, 1000):
                batch = [_row(i) for i in range(start, min(start+1000, rows))]
                assert table.insert_many(batch) == len(batch)
        timed('seed_insert', seed)
        table.kv.get(rows // 2)
        table.select({'group': 0}, limit=1)
        def reads():
            for _ in range(300):
                assert table.kv.get(rows // 2) == expected[rows // 2]
        timed('point_read', reads)
        if extended:
            def secondary_reads():
                for i in range(100):
                    assert table.select({'group': i % 16}, limit=1) == [expected[i % 16]]
            timed('secondary_equal_read', secondary_reads)
            def point_inserts():
                for i in range(rows+22000, rows+22020):
                    table.append(_row(i))
            timed('point_insert', point_inserts)
            expected.update({i: _row(i) for i in range(rows+22000, rows+22020)})
        def patches(use_kv):
            for i in range(20):
                value = (910000 if use_kv else 900000) + i
                count = (table.kv.update(rows//2, {'value': value}) if use_kv else
                         table.update_by_pk(rows//2, {'value': value}))
                assert count == 1
            expected[rows//2]['value'] = value
        timed('point_update', lambda: patches(False))
        timed('key_value_update', lambda: patches(True))
        def secondary():
            for i in range(50):
                assert table.update({'value': 920000+i}, {'group': i % 16}, limit=1) == 1
                expected[i % 16]['value'] = 920000+i
        timed('secondary_equal_update', secondary)
        def batch_read(pipeline):
            for _ in range(20):
                if pipeline:
                    pipe = table.read_pipeline()
                    for i in range(500):
                        pipe.get(i)
                    result = pipe.execute()
                    assert result == [expected[i] for i in range(500)]
                else:
                    result = table.kv.get_many(range(500))
                    assert result == {i: expected[i] for i in range(500)}
        timed('batch_read', lambda: batch_read(False))
        timed('read_pipeline', lambda: batch_read(True))
        updates = {i: {'value': 930000+i} for i in range(500)}
        assert timed('batch_update', lambda: table.kv.update_many(updates)) == 500
        for i,patch in updates.items():
            expected[i].update(patch)
        conditions = [({'id': i}, {'value': 940000+i}) for i in range(500)]
        assert timed('condition_batch_update', lambda: table.update_many_where(conditions)) == 500
        for i in range(500):
            expected[i]['value'] = 940000+i
        def transaction():
            with table.transaction() as tx:
                for i in range(500):
                    tx.update_by_pk(i, {'value': 950000+i})
        timed('transaction_update', transaction)
        for i in range(500):
            expected[i]['value'] = 950000+i
        assert timed('batch_insert', lambda: table.insert_many(_row(i) for i in range(rows, rows+2000))) == 2000
        expected.update({i: _row(i) for i in range(rows, rows+2000)})
        timed('multi_thread_read_write', lambda: _threads(table, rows))
        expected.update({i: _row(i) for i in range(rows+10000, rows+10040)})
        timed('multi_process_append', lambda: _processes(kind, root, url, durability, rows+20000, False))
        expected.update({i: _row(i) for i in range(rows+20000, rows+20100)})
        timed('multi_process_append_batched', lambda: _processes(kind, root, url, durability, rows+21000, True))
        expected.update({i: _row(i) for i in range(rows+21000, rows+21100)})
        def deletes():
            for i in range(20):
                assert table.delete_by_pk(i) == 1
        timed('point_delete', deletes)
        assert timed('batch_delete', lambda: table.kv.delete_many(range(20, 520))) == 500
        for i in range(520):
            expected.pop(i)
        actual = {r['id']: r for r in table.all()}
        assert len(table) == len(expected) and actual == expected, 'final content mismatch'
        digest = hashlib.sha256(json.dumps(sorted(actual.items()), sort_keys=True).encode()).hexdigest()
        db.close()
        # A new client must observe the same data and indexes.
        db = _db(kind, root, url, durability)
        assert db['records'].kv.get(rows-1) == expected[rows-1]
        assert len(db['records']) == len(expected)
        return {'seconds': seconds, 'final_rows': len(expected), 'sha256': digest, 'verified': True}
    finally:
        if kind == 'redis':
            _cleanup(db)
        db.close()


def run(rows=50000, redis_url='redis://127.0.0.1:6379/0', repeat=3, durability='always'):
    if rows < 1000 or repeat < 1:
        raise ValueError('rows must be >= 1000 and repeat >= 1')
    with RedisDatabase(redis_url=redis_url, durability=durability) as probe:
        config = probe.server_config()
        info = probe.redis.info('server')
        server = {'version': info['redis_version'], 'os': info['os'], 'config': config}
        client_version = __import__('redis').__version__
    runs = []
    for number in range(repeat):
        with tempfile.TemporaryDirectory(prefix='rtdb-compare-') as directory:
            root = Path(directory) / 'dat'
            _definition(root)
            result = {}
            order = ('file_mapped', 'redis') if number % 2 == 0 else ('redis', 'file_mapped')
            for kind in order:
                print(f'run {number+1}/{repeat}: {kind}', file=sys.stderr, flush=True)
                result[kind] = _run_backend(kind, root, rows, redis_url, durability)
            assert result['redis']['sha256'] == result['file_mapped']['sha256']
            result['order'] = list(order)
            runs.append(result)
    names = runs[0]['file_mapped']['seconds']
    medians = {kind: {name: statistics.median(r[kind]['seconds'][name] for r in runs) for name in names}
               for kind in ('file_mapped', 'redis')}
    ratio = {name: medians['file_mapped'][name] / medians['redis'][name] for name in names}
    return {'utc': datetime.now(timezone.utc).isoformat(),
            'parameters': {'rows': rows, 'repeat': repeat, 'durability': durability, 'seed_chunk': 1000,
                           'point_reads': 300, 'point_updates': 20, 'secondary_updates': 50,
                           'batch_keys': 500, 'batch_read_rounds': 20, 'batch_insert': 2000,
                           'threads': 8, 'reads_per_thread': 60, 'writes_per_thread': 5,
                           'processes': 4, 'writes_per_process': 25, 'point_deletes': 20, 'batch_deletes': 500},
            'environment': {'python': sys.version, 'platform': platform.platform(),
                            'redis_py': client_version, 'redis': server},
            'runs': runs, 'median_seconds': medians, 'speedup_file_divided_by_redis': ratio}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--redis-url', default='redis://127.0.0.1:6379/0')
    parser.add_argument('--rows', type=int, default=50000)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--durability', choices=('always', 'server'), default='always')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run(args.rows, args.redis_url, args.repeat, args.durability)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + '\n', encoding='utf-8')
    if args.json:
        print(rendered)
    else:
        for name, ratio in result['speedup_file_divided_by_redis'].items():
            print(f"{name:30s} file={result['median_seconds']['file_mapped'][name]:.6f}s "
                  f"redis={result['median_seconds']['redis'][name]:.6f}s  file/redis={ratio:.3f}x")


if __name__ == '__main__':
    mp.freeze_support()
    main()
