"""Isolated 50,000-row Redis query A/B comparison against the pre-optimization selector.

Starts only an owned Redis child with AOF always; never changes a shared server.
The frozen baseline changes only Lua candidate selection, keeping the current
Python API, schema, serialization, write implementation and server identical.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import redis
from core import RedisDatabase
from core.redis_db import _SCRIPT
from benchmarks.benchmark_redis_vs_file import _definition, _cleanup
from benchmarks.verify_redis_durability import _config_arg

BASELINE_CANDIDATES = r'''local function candidates(q)
    if q.limit == 0 then return {} end
    local ids
    if q.id then ids = {q.id}
    else
        local best, size = order, nil
        for _,cond in ipairs(q.eq or {}) do
            local k = ik(cond[1],cond[2]); typ(k, 'zset')
            local n = redis.call('ZCARD',k)
            if not size or n < size then size=n; best=k end
        end
        local stop = -1
        if #touched == 0 and #(q.eq or {}) <= 1 and q.limit and q.limit >= 0 then
            stop = (q.offset or 0) + q.limit - 1
        end
        ids = redis.call('ZRANGE', best, 0, stop)
        if #touched > 0 then
            local seen = {}; for _,id in ipairs(ids) do seen[id]=true end
            for _,id in ipairs(touched) do if not seen[id] then ids[#ids+1]=id end end
            table.sort(ids,function(a,b)
                return (scores[a] or tonumber(redis.call('ZSCORE', order,a)) or math.huge)
                     < (scores[b] or tonumber(redis.call('ZSCORE', order,b)) or math.huge)
            end)
        end
    end
    local result, skipped = {}, 0
    for _,id in ipairs(ids) do
        if matches(current(id),q) then
            if skipped < (q.offset or 0) then skipped=skipped+1
            else
                result[#result+1]=id
                if q.limit and q.limit >= 0 and #result >= q.limit then break end
            end
        end
    end
    return result
end
'''


def run(executable, output, *, repeat=3, calls=20, tests=False, only=None):
    executable = executable.resolve(strict=True)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    baseline = (_SCRIPT[:_SCRIPT.index('local function candidates(q)')] + BASELINE_CANDIDATES
                + _SCRIPT[_SCRIPT.index("if p.op == 'read' then"):])
    scripts = {'before': baseline, 'after': _SCRIPT}
    with tempfile.TemporaryDirectory(prefix='rtdb-intersection-') as tmp:
        root = Path(tmp)
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
        with (root / 'server.log').open('wb') as log:
            process = subprocess.Popen([str(executable), _config_arg(config)], cwd=executable.parent,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            client = redis.Redis.from_url(url, socket_timeout=.3, socket_connect_timeout=.3)
            try:
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise RuntimeError((root / 'server.log').read_text())
                    try:
                        if client.ping():
                            break
                    except redis.exceptions.RedisError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.05)
                _definition(root / 'dat')
                data = [{'id': i, 'group': i // 25000, 'value': i % 2, 'name': str(i // 25000)}
                        for i in range(50000)]
                scenarios = {
                    'late_match_limit_1': ({'value': 0, 'group': 1}, 1, 0),
                    'early_match_limit_1': ({'group': 1, 'value': 0}, 1, 0),
                    'empty_intersection': ({'group': 0, 'name': '1'}, 1, 0),
                    'dense_match_limit_1': ({'group': 0, 'name': '0'}, 1, 0),
                    'page_offset_1000': ({'value': 0, 'group': 1}, 20, 1000),
                    'primary_key_with_filter': ({'id': 123, 'group': 0}, 1, 0),
                    'single_equality_limit_1': ({'group': 0}, 1, 0),
                    'ordered_offset_40000': (None, 1, 40000),
                    'single_equality_offset_20000': ({'value': 0}, 1, 20000),
                    'single_equality_tail': ({'value': 0}, None, 24980),
                }
                if only:
                    unknown = set(only) - set(scenarios)
                    if unknown:
                        raise ValueError(f'unknown scenarios: {sorted(unknown)}')
                    scenarios = {name: scenarios[name] for name in only}
                with RedisDatabase(root / 'dat', redis_url=url) as db:
                    table = db['records']
                    try:
                        for start in range(0, len(data), 1000):
                            table.insert_many(data[start:start+1000])
                        runs = {name: {kind: [] for kind in scripts} for name in scenarios}
                        for iteration in range(repeat):
                            order = ('before', 'after') if iteration % 2 == 0 else ('after', 'before')
                            for kind in order:
                                table._script = db.redis.register_script(scripts[kind])
                                print(f'round {iteration+1}/{repeat} {kind}', flush=True)
                                for name, (where, limit, offset) in scenarios.items():
                                    expected = [r for r in data if all(r[k] == v for k,v in (where or {}).items())][offset:][:limit]
                                    assert table.select(where, limit=limit, offset=offset) == expected
                                    start = time.perf_counter()
                                    for _ in range(calls):
                                        assert table.select(where, limit=limit, offset=offset) == expected
                                    runs[name][kind].append(time.perf_counter()-start)
                        medians = {name: {kind: statistics.median(values) for kind,values in timings.items()}
                                   for name,timings in runs.items()}
                        result = {'rows': len(data), 'calls_per_round': calls, 'repeat': repeat,
                                  'redis_version': db.redis.info('server')['redis_version'],
                                  'config': db.server_config(), 'runs': runs, 'median_seconds': medians,
                                  'speedup_before_divided_by_after': {name: t['before']/t['after'] for name,t in medians.items()},
                                  'script_sha256': {kind: hashlib.sha256(text.encode()).hexdigest() for kind,text in scripts.items()},
                                  'verified': True}
                        output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
                        for kind, script in scripts.items():
                            output.with_name(f'{output.stem}-{kind}.lua').write_text(script, encoding='utf-8')
                        print(json.dumps(result, indent=2), flush=True)
                    finally:
                        _cleanup(db)
                assert client.dbsize() == 0, 'temporary namespace leaked'
                if tests:
                    env = dict(os.environ, RTDB_TEST_REDIS_URL=url)
                    subprocess.run([sys.executable, '-m', 'unittest', 'core.test_redis_db',
                        'core.test_file_mapped_db', 'core.test_table_schema', 'web.test_server', '-q'],
                        cwd=ROOT, env=env, check=True, timeout=180)
                    assert client.dbsize() == 0, 'test namespace leaked'
                    print('Full regression passed; isolated Redis key count after cleanup: 0', flush=True)
                client.shutdown(nosave=True)
                process.wait(timeout=10)
            finally:
                client.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--redis-server', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/redis-query-comparison.json')
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--calls', type=int, default=20)
    parser.add_argument('--tests', action='store_true', help='run full regression against the isolated server')
    parser.add_argument('--only', nargs='+', help='optional subset of scenario names')
    args = parser.parse_args()
    if args.repeat < 1 or args.calls < 1:
        parser.error('--repeat and --calls must be positive')
    run(args.redis_server, args.output, repeat=args.repeat, calls=args.calls, tests=args.tests, only=args.only)
