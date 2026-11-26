"""Isolated comparisons for save protocol, database scheduling and receipt compression."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.request import Request,urlopen
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from benchmarks.benchmark_delivery_indexes import definition,summary
from web.sqlite_server import create_sqlite_server
from web.sqlite_service import SQLiteManager
from web.management import SQLiteManagement
from web.tasks import TaskRegistry
from web.receipt_maintenance import archive_batch,statistics
from web import write_receipts


def saves(root):
    server=create_sqlite_server('127.0.0.1',0,root=root)
    server.sqlite_manager.create_database_file({'name':'lab','tables':[definition()]})
    manager=server.sqlite_manager.for_database('lab.db');manager.insert('samples',{'rows':[{'id':i,'v01':0} for i in range(1,501)]})
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    origin=f'http://127.0.0.1:{server.server_port}';groups=[]
    def call(method,path,body=None):
        request=Request(origin+path,method=method,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=15) as response:return json.load(response)
    try:
        for count in (1,100,500):
            samples={'previous':[],'current':[]}
            for iteration in range(-2,15):
                for mode in (('previous','current') if iteration%2 else ('current','previous')):
                    assert server.audit_delivery.drain(10)
                    records=SQLiteManagement(manager).rows('samples',{'limit':500})['records'][:count]
                    value=iteration*2+10000+(mode=='current')
                    payload={'changes':[{'ref':r['ref'],'values':{'v01':value}} for r in records]}
                    ident=uuid.uuid4().hex;requests=0;started=time.perf_counter()
                    if mode=='previous':
                        assert call('POST','/api/manage/tables/samples/validate-edits?database=lab.db',payload)['ok'];requests+=1
                    result=call('PUT','/api/manage/tables/samples/cell-edits?database=lab.db&background=1&request_id='+ident,payload);requests+=1
                    while 'task' in result:
                        task=result['task']
                        if task['status']=='done':result=task['result'];break
                        assert task['status'] in ('queued','running'),task
                        url='/api/manage/tasks/'+ident+'?database=lab.db'
                        if mode=='previous':time.sleep(.15)
                        else:url+='&wait=2&revision='+str(task.get('revision',0))
                        result={'task':call('GET',url)};requests+=1
                    saved=(time.perf_counter()-started)*1000
                    assert result['updated']==count
                    assert server.audit_delivery.drain(10)
                    logged=(time.perf_counter()-started)*1000
                    timings=server.tasks.get(ident,'lab.db')['timings']
                    assert all(r['row']['v01']==value for r in SQLiteManagement(manager).rows('samples',{'limit':500})['records'][:count])
                    if iteration>=0:samples[mode].append({'save_ms':saved,'log_complete_ms':logged,'requests':requests,'phases':timings})
            groups.append({'rows':count,'timings':{mode:{key:summary([s[key] for s in values]) for key in ('save_ms','log_complete_ms','requests')} for mode,values in samples.items()},'samples':samples})
        return groups
    finally:server.shutdown();server.server_close();thread.join()


def scheduling():
    samples={'fifo':[],'per_database':[]}
    for iteration in range(-2,15):
        for mode in (('fifo','per_database') if iteration%2 else ('per_database','fifo')):
            entered=threading.Event()
            def busy(*_):entered.set();time.sleep(.2);return {'ok':True}
            if mode=='fifo':
                with ThreadPoolExecutor(2) as pool:
                    pool.submit(busy);pool.submit(busy);assert entered.wait(2)
                    started=time.perf_counter();pool.submit(lambda:{'ok':True}).result();elapsed=(time.perf_counter()-started)*1000
            else:
                registry=TaskRegistry()
                def submit(database,callback):return registry.submit(SimpleNamespace(manager=SimpleNamespace(database_file=database),dispatch=callback),'POST',['definition-preview'],{}, {})
                try:
                    submit('a.db',busy);submit('a.db',busy);assert entered.wait(2)
                    started=time.perf_counter();task=submit('b.db',lambda *_:{'ok':True})
                    assert registry.wait_initial(task['id'],task['database'],2)['status']=='done';elapsed=(time.perf_counter()-started)*1000
                finally:registry.close()
            if iteration>=0:samples[mode].append(elapsed)
    return {'scope':'Two 200 ms operations on database A; completion delay of a short task on B, scheduler only, no HTTP/SQLite/task ledger',
            'timings':{mode:summary(values) for mode,values in samples.items()},'samples':samples}


def compression(root):
    manager=SQLiteManager(root/'data',root/'defs');manager.create_database_file({'name':'lab','tables':[definition()]})
    scoped=manager.for_database('lab.db');api=SQLiteManagement(scoped)
    try:
        api.dispatch('POST',['tables','samples','rows'],{}, {'rows':[{'id':1,'v01':1}]})
        with closing(sqlite3.connect(scoped.data_dir/scoped.database_file)) as c,c:
            result=json.dumps({'updated':500,'columns':['value_'+str(i) for i in range(40)]})
            c.execute('DELETE FROM rtdb_web_commits')
            for i in range(2000):
                event=json.dumps({'id':f'{i:032x}','timestamp':'2020-01-01T00:00:00Z','database':'lab.db','operation':'修改单元格数据','status':'success','result':{'updated':500}},ensure_ascii=False)
                c.execute('INSERT INTO rtdb_web_commits VALUES(?,?,?,?,1)',(f'{i:032x}','fixture',result,event))
        timings=[];batches=[]
        while True:
            start=time.perf_counter();batch=archive_batch(scoped);timings.append((time.perf_counter()-start)*1000);batches.append(batch['archived'])
            if not batch['more']:break
        stats=statistics(scoped)
        assert stats['archived']==2000 and max(batches)<=200
        assert write_receipts.receipt(scoped,'0'*32,'fixture')==json.loads(result)
        assert api.rows('samples',{})['total']==1
        return {'scope':'2000 synthetic delivered receipts; payload compression, not database file shrinkage; default 200-record/2MiB batches',
                **stats,'batches':batches,'batch_timing':summary(timings),'samples_ms':timings}
    finally:manager.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='rtdb-next-workflow-') as folder:
        root=Path(folder);result={'repeats':15,'warmups':2,'save':saves(root/'save'),'scheduling':scheduling(),'compression':compression(root/'compression')}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'save':[{'rows':r['rows'],'timings':r['timings']} for r in result['save']],
        'scheduling':result['scheduling']['timings'],'compression':{k:v for k,v in result['compression'].items() if k!='samples_ms'}},ensure_ascii=False))


if __name__=='__main__':main()
