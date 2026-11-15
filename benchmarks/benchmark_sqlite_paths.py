"""Alternate isolated before/after SQLite runs, including Web service hot paths.

--baseline-root points to an immutable source snapshot containing core/, web/
and benchmarks/. Each variant runs in a fresh interpreter with temporary data.
The core workloads are shared with benchmark_three_backends.py. Web timings
measure Python service calls (not HTTP, audit logging or browser rendering).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time


def worker(source_root, rows, tables):
    sys.path.insert(0, str(source_root))
    from benchmarks.benchmark_redis_vs_file import _definition, _row, _run_backend
    from core import SQLiteDatabase
    from web.management import SQLiteManagement
    from web.sqlite_service import SQLiteManager, fingerprint

    with tempfile.TemporaryDirectory(prefix='rtdb-sqlite-paths-') as folder:
        root = Path(folder)
        data = root / 'core/dat'
        _definition(data)
        core = _run_backend('sqlite', data, rows, '', 'always', extended=True)
        with SQLiteDatabase(data) as db:
            table = db['records']
            config = table.storage_config()
            assert config['journal_mode'] == 'wal' and config['synchronous'] == 2
            assert table._connection().execute('PRAGMA integrity_check').fetchone()[0] == 'ok'

        data = root / 'web/dat'
        schema = _definition(data)
        definition = json.loads((schema / 'records.json').read_text(encoding='utf-8'))
        for number in range(1, tables):
            definition['head']['eng_name'] = f'records_{number}'
            (schema / f'records_{number}.json').write_text(json.dumps(definition), encoding='utf-8')
        with SQLiteDatabase(data) as db:
            db['records'].insert_many(_row(i) for i in range(rows))
            for number in range(1, tables):
                db[f'records_{number}'].insert_one(_row(number))
        manager = SQLiteManager(data, schema)
        api = SQLiteManagement(manager)
        seconds = {}

        def timed(name, action, repeats=1):
            started = time.perf_counter()
            for _ in range(repeats):
                action()
            seconds[name] = time.perf_counter() - started

        def reopen():
            with SQLiteDatabase(data) as db:
                assert db['records'].kv.get(0) == _row(0)

        def table_list():
            result = manager.tables()['tables']
            assert len(result) == tables and all(not item['error'] for item in result)
            assert sum(item['row_count'] for item in result) == rows + tables - 1

        def page(filtered=False):
            result = manager.rows('records', where='"group"=15' if filtered else '',
                                  order='value DESC', limit=100)
            expected = list(range(15, rows, 16)) if filtered else range(rows)
            assert result['total'] == len(expected)
            assert [row['id'] for row in result['rows']] == list(reversed(expected))[:100]

        def sql_page():
            result = api.sql('records', {'sql': 'SELECT * FROM records', 'limit': 100})
            assert result['total'] == rows and len(result['records']) == 100
            assert result['records'][0]['row'] == _row(0)

        timed('reopen_read_20', reopen, 20)
        timed('table_list_5', table_list, 5)
        timed('page_20', page, 20)
        timed('filtered_page_20', lambda: page(True), 20)
        timed('sql_page_20', sql_page, 20)
        changes = [{'ref': {'key': [i], 'version': fingerprint(_row(i))},
                    'values': {'value': 700000 + i}} for i in range(500)]
        payload = {'changes': changes}
        def validate():
            assert api.validate_edits('records', payload) == {'ok': True, 'updated': 500, 'inserted': 0}
        def save():
            assert api.cell_edits('records', payload) == {'updated': 500}
        timed('validate_edits_500', validate)
        timed('save_edits_500', save)
        with SQLiteDatabase(data) as db:
            table = db['records']
            expected = [_row(i) for i in range(rows)]
            for i in range(500):
                expected[i]['value'] = 700000 + i
            assert table.all() == expected and len(table) == rows
            assert table._connection().execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
        return {'core': core, 'web': {'seconds': seconds, 'verified': True, 'sha256': digest},
                'storage_config': config, 'source_root': str(source_root),
                'source_sha256': {p: hashlib.sha256((source_root / p).read_bytes()).hexdigest()
                                  for p in ('core/sqlite_db.py', 'core/sqlite_schema.py',
                                            'web/sqlite_service.py', 'web/management.py')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--rows', type=int, default=50000)
    parser.add_argument('--tables', type=int, default=12)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.rows < 1000 or args.tables < 1 or args.repeat < 1:
        parser.error('rows >= 1000, tables >= 1 and repeat >= 1 are required')
    if args.worker:
        result = worker(args.source_root.resolve(), args.rows, args.tables)
    else:
        roots = {'candidate': args.source_root.resolve()}
        if args.baseline_root:
            roots = {'baseline': args.baseline_root.resolve(), **roots}
        runs = []
        with tempfile.TemporaryDirectory(prefix='rtdb-sqlite-ab-') as folder:
            for number in range(args.repeat):
                order = list(roots) if number % 2 == 0 else list(reversed(roots))
                run = {'order': order}
                for variant in order:
                    print(f'run {number + 1}/{args.repeat}: {variant}', flush=True)
                    destination = Path(folder) / f'{number}-{variant}.json'
                    subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker',
                        '--source-root', str(roots[variant]), '--rows', str(args.rows),
                        '--tables', str(args.tables), '--output', str(destination)],
                        check=True, cwd=roots[variant], timeout=300)
                    run[variant] = json.loads(destination.read_text(encoding='utf-8'))
                for category in ('core', 'web'):
                    assert len({run[v][category]['sha256'] for v in roots}) == 1
                runs.append(run)
        medians = {variant: {category: {name: statistics.median(
            run[variant][category]['seconds'][name] for run in runs)
            for name in runs[0][variant][category]['seconds']}
            for category in ('core', 'web')} for variant in roots}
        result = {'utc': datetime.now(timezone.utc).isoformat(),
                  'parameters': {'rows': args.rows, 'tables': args.tables, 'repeat': args.repeat},
                  'environment': {'python': sys.version, 'platform': platform.platform(),
                                  'sqlite': sqlite3.sqlite_version},
                  'notes': ['Each time is the total workload elapsed time, not per-request latency.',
                            'Core workloads are identical to benchmark_three_backends.py.',
                            'Web measurements exclude HTTP, audit logging and browser rendering.',
                            'Reopen/page/SQL workloads repeat 20 times; table list repeats 5 times.',
                            'All data is temporary; WAL/FULL and integrity checks are required.'],
                  'runs': runs, 'median_seconds': medians}
        if 'baseline' in roots:
            result['speedup'] = {category: {name: elapsed / medians['candidate'][category][name]
                for name, elapsed in medians['baseline'][category].items()} for category in ('core', 'web')}
            print(json.dumps(result['speedup'], indent=2))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
