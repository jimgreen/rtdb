"""Paired local E workflow timings; excludes HTTP and human confirmation."""
import base64
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

ROOT=Path(__file__).resolve().parents[1]


def case(count,kind,memory=False):
    with tempfile.TemporaryDirectory(prefix='rtdb-e-bench-') as folder:
        root=Path(folder);manager=SQLiteManager(root/'data',root/'defs')
        definitions=[dict(head=dict(eng_name=name,chn_name=name),columns=[
            dict(eng_name='id',chn_name='编号',data_type='int',primary_key=True),
            dict(eng_name='value',chn_name='数值',data_type='float',primary_key=False),
            dict(eng_name='label',chn_name='名称',data_type='str',primary_key=False)]) for name in ('meters','branches')]
        for definition in definitions:
            for column in definition['columns']:column.update(disp_type='文本',disp_menu='')
        manager.create_database_file(dict(name='grid',tables=definitions))
        scoped=manager.for_database('grid.db');api=SQLiteManagement(scoped)
        try:
            for definition in definitions:
                scoped.insert(definition['head']['eng_name'],{'rows':[dict(id=i,value=i/10,label='old') for i in range(count)]})
            path=root/'grid.e'
            with path.open('w',encoding='utf-8') as stream:
                for name in ('meters','branches'):
                    stream.write(f'<{name}>\n@ id value label\n')
                    for i in range(count//2,count+count//2):stream.write(f"# {i} {i/20} '测点{'x'*48}'\n")
                    stream.write(f'</{name}>\n')
            gc.collect()
            if memory:tracemalloc.start()
            started=time.perf_counter()
            if kind=='legacy':
                payload=dict(content=base64.b64encode(path.read_bytes()).decode(),format='e',encoding='auto',mode='merge',source_name='grid.e')
                preview=api.dispatch('POST',['definition-preview'],{},payload)
                ready=time.perf_counter()
                result=api.dispatch('POST',['definition-import'],{},{**payload,'token':preview['token']})
            else:
                ident=uuid.uuid4().hex
                uploads.create(manager,dict(id=ident,kind='e',filename='grid.e',size=path.stat().st_size,database='grid.db'))
                with path.open('rb') as stream:
                    offset=0
                    while block:=stream.read(uploads.CHUNK):
                        uploads.put_chunk(manager,ident,offset,block,hashlib.sha256(block).hexdigest());offset+=len(block)
                uploads.finalize(manager,ident)
                preview=api.dispatch('POST',['definition-preview'],{},dict(upload_id=ident,mode='merge'))
                ready=time.perf_counter()
                result=api.dispatch('POST',['definition-import'],{},dict(upload_id=ident,token=preview['token']))
            finished=time.perf_counter();peak=tracemalloc.get_traced_memory()[1] if memory else None
            if memory:tracemalloc.stop()
            assert result['ok'],result
            for name in ('meters','branches'):
                with scoped.session(name) as table:
                    assert len(table)==count+count//2
                    connection=table._connection()
                    row=connection.execute(f'SELECT _pos,c1,c2 FROM {table._data_sql} WHERE c0=?',(count//2,)).fetchone()
                    assert row[0]==count//2+1 and row[1]==count/40 and row[2].startswith('测点')
                    assert connection.execute(f'SELECT c2 FROM {table._data_sql} WHERE c0=0').fetchone()[0]=='old'
            return dict(total_ms=(finished-started)*1000,prepare_ms=(ready-started)*1000,apply_ms=(finished-ready)*1000,peak_bytes=peak,file_bytes=path.stat().st_size)
        finally:
            if tracemalloc.is_tracing():tracemalloc.stop()
            manager.close()


def main():
    report=dict(created=time.strftime('%Y-%m-%d %H:%M:%S'),python=platform.python_version(),platform=platform.platform(),sqlite=sqlite3.sqlite_version,
        method='2 tables, each N existing and N incoming, 50% overlap. One warmup per path, 3 alternating repeats. Separate tracemalloc pass. Includes local upload persistence, parsing, previews and commit; excludes HTTP, UI and fixture/result checks.',results=[])
    for count in (1000,10000,25000):
        print('rows per table',count,flush=True)
        for kind in ('legacy','streamed'):case(count,kind)
        samples={kind:[] for kind in ('legacy','streamed')}
        for repeat in range(3):
            for kind in (('legacy','streamed') if repeat%2==0 else ('streamed','legacy')):
                result=case(count,kind);samples[kind].append(result);print(kind,repeat,round(result['total_ms'],1),flush=True)
        result={}
        for kind in samples:
            measurement=case(count,kind,True)
            result[kind]={key:statistics.median(row[key] for row in samples[kind]) for key in ('total_ms','prepare_ms','apply_ms')}
            result[kind]['peak_bytes']=measurement['peak_bytes']
        report['results'].append(dict(rows_per_table=count,total_incoming=count*2,file_bytes=measurement['file_bytes'],results=result,samples=samples))
        (ROOT/'output/efile-stream-performance-20261115.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report['results'],indent=2),flush=True)


if __name__=='__main__':main()
