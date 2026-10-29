"""Same durable CRUD workload for mmap, Redis and SQLite; validate complete data.

Use --redis-server to start an isolated AOF-always Redis child, or --redis-url
for an already configured server. Only random namespaces are cleaned.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import RedisDatabase, SQLiteDatabase
from benchmarks.benchmark_redis_vs_file import _definition, _run_backend
from benchmarks.verify_redis_durability import _config_arg


@contextmanager
def isolated_redis(executable):
    """Own the child lifetime and data directory; bounded connection timeouts."""
    import redis
    from redis.retry import Retry
    from redis.backoff import NoBackoff
    executable = executable.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='rtdb-three-redis-') as temporary:
        root = Path(temporary)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = f'redis://127.0.0.1:{port}/0'
        config = root / 'redis.conf'
        config.write_text('\n'.join([
            'bind 127.0.0.1', f'port {port}', 'protected-mode yes', 'save ""',
            'appendonly yes', 'appendfsync always', 'no-appendfsync-on-rewrite no',
            'maxmemory-policy noeviction', 'daemonize no', f'dir "{root.as_posix()}"',
        ]) + '\n', encoding='utf-8')
        with (root / 'redis.log').open('wb') as log:
            child = subprocess.Popen([str(executable), _config_arg(config)], cwd=executable.parent,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            client = redis.Redis.from_url(url, socket_timeout=.3, socket_connect_timeout=.3,
                                          retry=Retry(NoBackoff(), 0))
            try:
                deadline = time.monotonic() + 20
                while True:
                    if child.poll() is not None:
                        raise RuntimeError((root / 'redis.log').read_text(errors='replace'))
                    try:
                        if client.ping():
                            break
                    except redis.RedisError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.05)
                yield url
                remaining = client.dbsize()
                if remaining:
                    raise AssertionError(f'test namespace leak: {remaining} keys')
                client.shutdown(nosave=True)
                child.wait(timeout=10)
            finally:
                client.close()
                if child.poll() is None:
                    child.kill(); child.wait(timeout=10)


def run(rows, repeat, url):
    if rows < 1000 or repeat < 1:
        raise ValueError('rows must be >= 1000 and repeat must be >= 1')
    with RedisDatabase(redis_url=url) as probe:
        info = probe.redis.info('server')
        redis_env = {'version': info['redis_version'], 'os': info['os'], 'config': probe.server_config()}
    backends = ('file_mapped', 'redis', 'sqlite')
    runs = []
    sqlite_config = None
    for number in range(repeat):
        with tempfile.TemporaryDirectory(prefix='rtdb-three-') as temporary:
            root = Path(temporary) / 'dat'
            _definition(root)
            order = backends[number % 3:] + backends[:number % 3]
            result = {'order': order}
            for kind in order:
                print(f'run {number+1}/{repeat}: {kind}', file=sys.stderr, flush=True)
                result[kind] = _run_backend(kind, root, rows, url, 'always', extended=True)
            assert len({result[k]['sha256'] for k in backends}) == 1, 'backend content mismatch'
            with SQLiteDatabase(root) as db:
                table = db['records']
                sqlite_config = table.storage_config()
                assert table._connection().execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert table._connection().execute('SELECT COUNT(*) FROM data').fetchone()[0] == len(table)
            runs.append(result)
    names = runs[0]['file_mapped']['seconds']
    medians = {k: {n: statistics.median(r[k]['seconds'][n] for r in runs) for n in names} for k in backends}
    ratios = {k: {n: medians['file_mapped'][n] / medians[k][n] for n in names} for k in ('redis', 'sqlite')}
    return {'utc': datetime.now(timezone.utc).isoformat(),
            'parameters': {'rows': rows, 'repeat': repeat, 'seed_chunk': 1000, 'point_reads': 300,
                           'secondary_reads': 100, 'point_inserts': 20, 'point_updates': 20,
                           'secondary_updates': 50, 'batch_keys': 500, 'batch_read_rounds': 20,
                           'batch_insert': 2000, 'threads': 8, 'reads_per_thread': 60,
                           'writes_per_thread': 5, 'processes': 4, 'writes_per_process': 25,
                           'point_deletes': 20, 'batch_deletes': 500},
            'environment': {'python': sys.version, 'platform': platform.platform(),
                            'redis_py': __import__('redis').__version__, 'redis': redis_env,
                            'sqlite': {'version': sqlite3.sqlite_version, 'config': sqlite_config}},
            'runs': runs, 'median_seconds': medians, 'speedup_vs_file_mapped': ratios,
            'winner': {n: min(backends, key=lambda k: medians[k][n]) for n in names}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    server = parser.add_mutually_exclusive_group(required=True)
    server.add_argument('--redis-url')
    server.add_argument('--redis-server', type=Path)
    parser.add_argument('--rows', type=int, default=50000)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--tests', action='store_true', help='run all backend/Web regression tests')
    parser.add_argument('--output', type=Path, default=Path('output/three-backends-50000.json'))
    args = parser.parse_args()
    def compare(url):
        if args.tests:
            subprocess.run([sys.executable, '-m', 'unittest', 'core.test_sqlite_db', 'core.test_sqlite_writer', 'core.test_redis_db', 'core.test_file_mapped_writer',
                            'core.test_file_mapped_db', 'core.test_table_schema', 'web.test_server', '-q'],
                           cwd=Path(__file__).resolve().parents[1], check=True, timeout=180,
                           env=dict(os.environ, RTDB_TEST_REDIS_URL=url))
        return run(args.rows, args.repeat, url)
    if args.redis_server:
        with isolated_redis(args.redis_server) as url:
            result = compare(url)
    else:
        result = compare(args.redis_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    for name in result['winner']:
        times = ' '.join(f'{k}={v[name]:.6f}s' for k,v in result['median_seconds'].items())
        print(f'{name:30s} {times} winner={result["winner"][name]}')
    print(f'Raw results: {args.output.resolve()}')


if __name__ == '__main__':
    mp.freeze_support()
    main()
