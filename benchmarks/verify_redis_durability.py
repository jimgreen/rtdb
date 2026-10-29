"""Start an isolated server, verify process hot sync and AOF crash recovery.

Requires an explicit redis-server executable. Only owned child processes and
an isolated temporary directory are used. This is process-crash testing, not
proof against physical power loss or faulty storage hardware.
"""
from __future__ import annotations
import argparse
import json
import multiprocessing as mp
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import RedisDatabase
from benchmarks.benchmark_redis_vs_file import _definition, _row


def writer(root, url):
    with RedisDatabase(root, redis_url=url) as db:
        db['records'].update_by_pk(7, {'value': 999})
        db['records'].append(_row(1000))


def _config_arg(path):
    value = str(path.resolve())
    # redis-windows MSYS2/Cygwin executables use POSIX arguments.
    if os.name == 'nt':
        return '/cygdrive/' + value[0].lower() + value[2:].replace('\\', '/')
    return value


def run(executable):
    executable = executable.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='rtdb-aof-verify-') as temp:
        root = Path(temp).resolve()
        _definition(root / 'dat')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = f'redis://127.0.0.1:{port}/0'
        config = root / 'redis.conf'
        config.write_text('\n'.join([
            'bind 127.0.0.1', f'port {port}', 'protected-mode yes',
            'save ""', 'appendonly yes', 'appendfsync always',
            'no-appendfsync-on-rewrite no', 'maxmemory-policy noeviction',
            'daemonize no', f'dir "{root.as_posix()}"',
        ]) + '\n', encoding='utf-8')
        processes = []
        def start(log):
            process = subprocess.Popen([str(executable), _config_arg(config)],
                cwd=executable.parent, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            processes.append(process)
            import redis
            client = redis.Redis.from_url(url, socket_timeout=0.2, socket_connect_timeout=0.2)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('isolated Redis exited during startup')
                try:
                    if client.ping():
                        client.close()
                        return process
                except redis.exceptions.RedisError:
                    time.sleep(0.05)
            raise RuntimeError('isolated Redis startup timed out')
        with (root / 'console.log').open('wb') as log:
            try:
                first = start(log)
                with RedisDatabase(root / 'dat', redis_url=url) as db:
                    table = db['records']
                    table.insert_many(_row(i) for i in range(100))
                    assert table.kv.get(7)['value'] == 14
                    child = mp.get_context('spawn').Process(target=writer, args=(str(root/'dat'), url))
                    child.start()
                    child.join(30)
                    if child.is_alive():
                        child.kill(); child.join()
                        raise RuntimeError('writer timed out')
                    assert child.exitcode == 0
                    assert table.kv.get(7)['value'] == 999  # Same already-open handle.
                    assert table.kv.get(1000) == _row(1000)
                    table.kv.update_many({i: {'group': 77} for i in range(20)})
                    table.delete_by_pk(99)
                    before = table.all()
                    config_before = db.server_config()
                # No SHUTDOWN/SAVE: force termination after acknowledged commits.
                first.kill()
                first.wait(timeout=15)
                second = start(log)
                with RedisDatabase(root / 'dat', redis_url=url) as recovered:
                    assert recovered['records'].all() == before
                    assert len(recovered['records'].select({'group': 77})) == 20
                    assert recovered['records'].kv.get(99) is None
                    info = recovered.redis.info('server')
                    recovered.redis.shutdown(nosave=True)
                second.wait(timeout=15)
                return {'passed': True, 'records_after_recovery': len(before),
                        'hot_sync_without_reopen': True, 'forced_process_termination_recovery': True,
                        'redis_version': info['redis_version'], 'config': config_before,
                        'scope': 'process crash and AOF restart; physical power-loss not tested'}
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--redis-server', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run(args.redis_server)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    mp.freeze_support()
    main()
