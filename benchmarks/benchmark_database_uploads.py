"""Isolated HTTP upload comparison; both paths finish with a new database."""
import base64
from contextlib import closing
from datetime import datetime
import gc
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from urllib.request import Request, urlopen
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager
from web.sqlite_server import create_sqlite_server, SQLiteRequestHandler
from web import database_snapshots as snapshots
from web.database_uploads import CHUNK
from web.test_sqlite_multi_database import table_definition


def summary(values):
    return dict(median_ms=statistics.median(values), min_ms=min(values), max_ms=max(values), samples_ms=values)


def measure(mib, repeats):
    with tempfile.TemporaryDirectory(prefix='rtdb-upload-benchmark-') as temp:
        root=Path(temp);manager=SQLiteManager(root/'source',root/'definitions')
        manager.create_database_file({'name':'source','tables':[table_definition('nodes','节点','id'),table_definition('lines','线路','id')]})
        for name in ('nodes','lines'):
            manager.for_database('source.sqlite3').insert(name,{'rows':[{'id':i,'name':'测点'+str(i)} for i in range(1000)]})
        with closing(sqlite3.connect(root/'source/source.sqlite3')) as connection:
            connection.execute('CREATE TABLE attachment(content BLOB)')
            connection.execute('INSERT INTO attachment VALUES(zeroblob(?))',(mib*1024**2,));connection.commit()
        item=snapshots.create(manager.for_database('source.sqlite3'))
        source=snapshots.resolve(manager.for_database('source.sqlite3'),item['id'])[0]
        size=source.stat().st_size;manager.close()
        server=create_sqlite_server('127.0.0.1',0,data_dir=root/'target',schema_dir=root/'target-defs')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        origin=f'http://127.0.0.1:{server.server_port}/api/manage/'
        wire=0;serial=0
        def call(path, payload=None, binary=None, headers=None):
            nonlocal wire
            data=binary if binary is not None else (json.dumps(payload).encode() if payload is not None else None)
            wire+=len(data or b'')
            req=Request(origin+path,data=data,method='PUT' if binary is not None else 'POST' if payload is not None else 'GET',
                        headers=headers or {'Content-Type':'application/json'})
            with urlopen(req,timeout=120) as response:return json.load(response)
        def execute(method):
            nonlocal serial,wire
            serial+=1;wire=0;name=f'{method}_{serial}';started=time.perf_counter()
            if method=='base64':
                encoded=base64.b64encode(source.read_bytes()).decode('ascii')
                call('database-files/upload',{'files':[{'name':name+'.sqlite3','content':encoded}]})
            else:
                ident=uuid.uuid4().hex
                call('database-uploads',{'id':ident,'filename':source.name,'size':size})
                with source.open('rb') as stream:
                    for offset in range(0,size,CHUNK):
                        block=stream.read(CHUNK)
                        call(f'database-uploads/{ident}/chunk?offset={offset}',binary=block,
                             headers={'Content-Type':'application/octet-stream','X-Chunk-SHA256':hashlib.sha256(block).hexdigest()})
                result=call(f'database-uploads/{ident}/finalize',{})
                call(f'database-snapshots/{ident}/restore?database={result["database"]}',{'name':name})
            elapsed=(time.perf_counter()-started)*1000
            with closing(sqlite3.connect(root/'target'/(name+'.sqlite3'))) as connection:
                assert connection.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                for table in ('nodes','lines'):
                    assert connection.execute(f'SELECT COUNT(*) FROM rtdb_data_{table}').fetchone()[0]==1000
                assert connection.execute('SELECT length(content) FROM attachment').fetchone()[0]==mib*1024**2
            return elapsed,wire
        try:
            methods=['base64','chunked'] if size<=16*1024**2 else ['chunked']
            values={method:[] for method in methods};bodies={}
            for method in methods:execute(method)  # warmup, excluded
            for repeat in range(repeats):
                for method in (methods if repeat%2==0 else list(reversed(methods))):
                    gc.collect();elapsed,bodies[method]=execute(method);values[method].append(elapsed)
            memory={}
            for method in methods:
                gc.collect();tracemalloc.start();execute(method)
                memory[method]=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
            return dict(payload_mib=mib,file_bytes=size,repeats=repeats,
                        timings={method:summary(times) for method,times in values.items()},
                        request_body_bytes=bodies,python_peak_bytes=memory,verified=True,
                        old_supported='base64' in methods)
        finally:
            server.shutdown();server.server_close();thread.join(3)


def main():
    SQLiteRequestHandler.log_message=lambda *_args:None
    result=dict(created=datetime.now().astimezone().isoformat(),python=platform.python_version(),sqlite=sqlite3.sqlite_version,
                platform=platform.platform(),transport='HTTP loopback, sequential requests',
                memory_scope='Python client and server allocations in same process; excludes native SQLite and browser memory',results=[])
    for size,repeats in [(8,5),(32,3),(64,3)]:
        row=measure(size,repeats);result['results'].append(row);print(json.dumps(row),flush=True)
    path=Path('output/database-upload-performance-20261115.json')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
