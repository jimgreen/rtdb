"""Paired, isolated HTTP-save and SQLite index-policy benchmarks."""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import SQLiteDatabase
from web.management import SQLiteManagement
from web.sqlite_server import create_sqlite_server
from web.sqlite_service import SQLiteManager


def summary(values):
    return dict(median_ms=round(statistics.median(values),3),p95_ms=round(sorted(values)[math.ceil(.95*len(values))-1],3))


def definition(columns=2, indexes=None):
    names=['id']+[f'v{i:02}' for i in range(1,columns)]
    result={'head':{'eng_name':'samples','chn_name':'样本'},'columns':[
        dict(eng_name=name,chn_name=name,data_type='int',primary_key=name=='id',disp_type='整数',disp_menu='') for name in names]}
    if indexes is not None:result['indexes']=indexes
    return result


def http_benchmark(root):
    server=create_sqlite_server('127.0.0.1',0,root=root)
    manager=server.sqlite_manager;manager.create_database_file({'name':'lab','tables':[definition()]})
    manager.for_database('lab.db').insert('samples',{'rows':[{'id':i,'v01':0} for i in range(1,501)]})
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    origin=f'http://127.0.0.1:{server.server_port}';delivery=server.audit_delivery
    def call(method,path,payload=None):
        request=Request(origin+path,method=method,data=json.dumps(payload).encode() if payload is not None else None,headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=20) as response:return json.load(response)
    results=[]
    try:
        for size in (1,100,500):
            samples={mode:[] for mode in ('sync','background')}
            for iteration in range(-2,15):
                for mode in (('sync','background') if iteration%2 else ('background','sync')):
                    assert delivery.drain(10)
                    manager.audit_delivery=delivery if mode=='background' else None
                    records=SQLiteManagement(manager.for_database('lab.db')).rows('samples',{'limit':500})['records'][:size]
                    value=10000+size*100+iteration*2+(mode=='background')
                    payload={'changes':[{'ref':r['ref'],'values':{'v01':value}} for r in records]}
                    start=time.perf_counter()
                    validated=call('POST','/api/manage/tables/samples/validate-edits?database=lab.db',payload)
                    assert validated['ok']
                    result=call('PUT','/api/manage/tables/samples/cell-edits?database=lab.db&background=1&request_id='+uuid.uuid4().hex,payload)
                    while 'task' in result:
                        task=result['task']
                        if task['status']=='done':result=task['result'];break
                        assert task['status'] in ('running','queued'),task
                        time.sleep(.15)
                        result={'task':call('GET','/api/manage/tasks/'+task['id']+'?database=lab.db')}
                    saved=(time.perf_counter()-start)*1000
                    assert result['updated']==size
                    assert delivery.drain(10)
                    synced=(time.perf_counter()-start)*1000
                    assert all(r['row']['v01']==value for r in SQLiteManagement(manager.for_database('lab.db')).rows('samples',{'limit':500})['records'][:size])
                    if iteration>=0:samples[mode].append(dict(save_ms=saved,including_log_delivery_ms=synced))
            results.append(dict(rows=size,timings={mode:{key:summary([item[key] for item in values]) for key in values[0]} for mode,values in samples.items()},samples=samples))
        return dict(scope='HTTP validate + background task save + task ledger + JSON decode + 150 ms task polling when needed; separately includes audit delivery; excludes browser rendering',results=results)
    finally:
        manager.audit_delivery=delivery;server.shutdown();server.server_close();thread.join()


def index_benchmark(root):
    rows=[{'id':i,**{f'v{j:02}':i%100 if j==1 else i%1000 if j==2 else (i*j)%997 for j in range(1,21)}} for i in range(1,20001)]
    managers={};databases={};tables={};samples={mode:{'batch_update_ms':[],'indexed_query_ms':[]} for mode in ('all_columns','selected')}
    try:
        for mode in samples:
            manager=managers[mode]=SQLiteManager(root/mode/'data',root/mode/'defs')
            manager.create_database_file({'name':'lab','tables':[definition(21,None if mode=='all_columns' else [['v01','v02']])]})
            db=databases[mode]=SQLiteDatabase(manager.data_dir,schema_directory=manager.schema_dir,database_file='lab.db');table=tables[mode]=db['samples']
            table.insert_many(rows)
        for iteration in range(-2,15):
            query_results=[]
            for mode in (('all_columns','selected') if iteration%2 else ('selected','all_columns')):
                table=tables[mode];start=time.perf_counter()
                with table.transaction() as batch:
                    for key in range(1,501):batch.update_by_pk(key,{f'v{j:02}':key+j+iteration+1000 for j in range(3,21)})
                updated=(time.perf_counter()-start)*1000
                start=time.perf_counter();result=table._connection().execute(f'SELECT c0,c1,c2 FROM {table._data_sql} WHERE c1=? AND c2>=? ORDER BY c2,c0',(42,100)).fetchall();queried=(time.perf_counter()-start)*1000
                query_results.append(result)
                if iteration>=0:samples[mode]['batch_update_ms'].append(updated);samples[mode]['indexed_query_ms'].append(queried)
            assert query_results[0]==query_results[1]
        storage={}
        for mode,table in tables.items():
            c=table._connection();c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            storage[mode]=dict(bytes=c.execute('PRAGMA page_count').fetchone()[0]*c.execute('PRAGMA page_size').fetchone()[0],
                indexes=len(c.execute(f'PRAGMA index_list({table._data_sql})').fetchall()),rows=len(table),
                query_plan=[row[3] for row in c.execute(f'EXPLAIN QUERY PLAN SELECT c0,c1,c2 FROM {table._data_sql} WHERE c1=42 AND c2>=100 ORDER BY c2,c0')])
        assert all(item['rows']==20000 for item in storage.values())
        return dict(scope='SQLite core 20000 rows x 21 columns, 500-row transaction updates 18 nonfilter columns; query filters v01/v02. Excludes HTTP/audit/UI. Selected indexes trade other-field search performance for write cost.',
                    timings={mode:{key:summary(values) for key,values in group.items()} for mode,group in samples.items()},storage=storage,samples=samples)
    finally:
        for db in databases.values():db.close()
        for manager in managers.values():manager.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='rtdb-delivery-index-bench-') as folder:
        root=Path(folder)
        result=dict(warmups=2,repeats=15,http=http_benchmark(root/'http'),indexes=index_benchmark(root/'indexes'))
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(dict(http=[dict(rows=row['rows'],timings=row['timings']) for row in result['http']['results']],indexes=result['indexes']['timings'],storage=result['indexes']['storage']),ensure_ascii=False))


if __name__=='__main__':main()
