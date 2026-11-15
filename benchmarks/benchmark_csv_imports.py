"""Paired legacy vs staged CSV merge, temporary databases only."""
from contextlib import closing
from datetime import datetime
import csv
import gc
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager
from web.management import SQLiteManagement
from web import database_uploads as uploads
from web.test_sqlite_multi_database import table_definition


def measure(count,repeats=3):
    with tempfile.TemporaryDirectory(prefix='rtdb-csv-bench-') as temp:
        root=Path(temp);manager=SQLiteManager(root/'data',root/'defs');source=root/'source.csv'
        with source.open('w',encoding='utf-8',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['id','name'])
            for i in range(count//2,count+count//2):writer.writerow([i,'new_'+'x'*160])
        serial=0
        def run(method,memory=False):
            nonlocal serial
            serial+=1;database=f'case_{serial}.sqlite3'
            manager.create_database_file({'name':database[:-8],'tables':[table_definition('nodes','节点','id')]})
            scoped=manager.for_database(database);api=SQLiteManagement(scoped)
            with scoped.session('nodes') as table:
                for start in range(0,count,2000):table.insert_many([{'id':i,'name':'old'} for i in range(start,min(start+2000,count))])
            gc.collect()
            if memory:tracemalloc.start()
            started=time.perf_counter()
            if method=='legacy':
                payload=dict(format='csv',text=source.read_text(encoding='utf-8'),mode='merge',confirm='nodes')
            else:
                ident=uuid.uuid4().hex;size=source.stat().st_size
                uploads.create(manager,dict(id=ident,kind='csv',filename='source.csv',size=size,database=database,table='nodes'))
                with source.open('rb') as stream:
                    for offset in range(0,size,uploads.CHUNK):
                        block=stream.read(uploads.CHUNK);uploads.put_chunk(manager,ident,offset,block,hashlib.sha256(block).hexdigest())
                preview=uploads.finalize(manager,ident)
                payload=dict(upload_id=ident,token=preview['token'],mode='merge',confirm='nodes')
            prepared=time.perf_counter()
            result=api.dispatch('POST',['tables','nodes','import'],{},payload)
            ended=time.perf_counter();peak=tracemalloc.get_traced_memory()[1] if memory else None
            if memory:tracemalloc.stop()
            assert result['imported']==count
            with scoped.session('nodes') as table:
                assert len(table)==count+count//2
                assert table.kv.get(count//2)['name'].startswith('new_')
                assert table.kv.get(0)['name']=='old'
                assert table._connection().execute(f'SELECT _pos FROM {table._data_sql} WHERE c0=?',(count//2,)).fetchone()[0]==count//2+1
            return dict(total_ms=(ended-started)*1000,prepare_ms=(prepared-started)*1000,apply_ms=(ended-prepared)*1000,peak_bytes=peak)
        try:
            samples={kind:[] for kind in ('legacy','streamed')}
            for kind in samples:run(kind)
            for repeat in range(repeats):
                for kind in (list(samples) if repeat%2==0 else list(reversed(samples))):samples[kind].append(run(kind))
            result=dict(rows=count,file_bytes=source.stat().st_size,repeats=repeats,results={},verified=True)
            for kind in samples:
                result['results'][kind]={'samples':samples[kind],**{key:statistics.median(s[key] for s in samples[kind]) for key in ('total_ms','prepare_ms','apply_ms')},'peak_bytes':run(kind,True)['peak_bytes']}
            return result
        finally:manager.close()


def main():
    result=dict(created=datetime.now().astimezone().isoformat(),platform=platform.platform(),python=platform.python_version(),sqlite=sqlite3.sqlite_version,
        scope='Same-process local service calls, excludes HTTP and browser; includes staged upload disk writes, SHA, preview and final import; setup and verification excluded.',results=[])
    for count in (1000,10000,50000):
        item=measure(count);result['results'].append(item);print(json.dumps(item),flush=True)
    Path('output/csv-import-performance-20261115.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
