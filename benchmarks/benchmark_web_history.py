"""Isolated log browsing and 500-row edit timings, with result fingerprints."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.audit import OperationAuditLog
from web.management import SQLiteManagement
from web.sqlite_service import SQLiteManager


def sample(action, repeats):
    durations = []
    value = None
    for _ in range(repeats):
        start = time.perf_counter()
        value = action()
        durations.append((time.perf_counter() - start) * 1000)
    return {'median_ms': round(statistics.median(durations), 3),
            'p95_ms': round(sorted(durations)[int(.95 * (repeats - 1))], 3)}, value


def run(rows=100000, repeats=15):
    with tempfile.TemporaryDirectory(prefix='rtdb-history-bench-') as folder:
        root = Path(folder)
        audit = OperationAuditLog(root / 'logs')
        for day in range(1, 6):
            path = audit.log_dir / f'2026-11-{day:02}.jsonl'
            with path.open('w', encoding='utf-8', newline='') as stream:
                for number in range(day - 1, rows, 5):
                    record = dict(id=f'event-{number:08}', timestamp=f'2026-11-{day:02}T12:00:00+13:00',
                        category='data' if number % 3 else 'schema', operation='修改记录',
                        status='failure' if number % 7 == 0 else 'success', table='devices',
                        error='断线重试 Unicode Straße %_' if number % 7 == 0 else None,
                        duration_ms=2.5, result={'updated': 1}, path='/api/manage/tables/devices/rows')
                    stream.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        source_bytes = sum(p.stat().st_size for p in audit.log_dir.glob('*.jsonl'))
        cold, _ = sample(lambda: audit.query(page_size=50), 1)
        cases = {'latest_50': {}, 'deep_page_1000': {'page': 1000},
                 'date_failures': {'date_value': '2026-11-03', 'status': 'failure'},
                 'keyword_failures': {'category': 'data', 'status': 'failure', 'keyword': 'STRASSE %_'}}
        timings, results = {}, {}
        for name, args in cases.items():
            timings[name], value = sample(lambda: audit.query(page_size=50, **args), repeats)
            results[name] = {'total': value['total'], 'count': value['count'],
                'digest': hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        # Querying through a new owner proves whether an index survives service restart.
        timings['reopen_latest_50'], _ = sample(lambda: OperationAuditLog(root / 'logs').query(page_size=50), repeats)
        manager = SQLiteManager(root / 'data', root / 'defs')
        try:
            definition = {'head': {'eng_name': 'samples', 'chn_name': '采样'}, 'columns': [
                dict(eng_name=n, chn_name=n, data_type=t, primary_key=k, disp_type='文本', disp_menu='')
                for n, t, k in [('id', 'int', True), ('value', 'float', False)]]}
            manager.create_database_file({'name': 'bench', 'tables': [definition]})
            scoped = manager.for_database('bench.db'); api = SQLiteManagement(scoped)
            scoped.insert('samples', {'rows': [{'id': i, 'value': float(i)} for i in range(10000)]})
            edits = []
            for repeat in range(repeats):
                records = api.rows('samples', {'limit': 500})['records']
                payload = {'changes': [{'ref': r['ref'], 'values': {'value': repeat + .25}} for r in records]}
                start = time.perf_counter(); outcome = api.cell_edits('samples', payload)
                edits.append((time.perf_counter() - start) * 1000)
                assert outcome['updated'] == 500
            timings['save_500_rows'] = {'median_ms': round(statistics.median(edits), 3),
                'p95_ms': round(sorted(edits)[int(.95 * (repeats - 1))], 3)}
        finally:
            manager.close()
        return dict(log_records=rows, log_files=5, source_bytes=source_bytes, repeats=repeats,
                    python=platform.python_version(), platform=platform.platform(),
                    scope='service layer; JSONL reads and SQLite commits included; excludes HTTP',
                    cold_first_query=cold, timings=timings, results=results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rows', type=int, default=100000)
    parser.add_argument('--repeats', type=int, default=15)
    args = parser.parse_args()
    result = run(args.rows, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
