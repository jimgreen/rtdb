"""Repeatable service-layer timings; only creates temporary test databases."""
import argparse
import json
from pathlib import Path
import statistics
import tempfile
import threading
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager
from web.management import SQLiteManagement


def run(rows=10000, repeats=30):
    with tempfile.TemporaryDirectory(prefix='rtdb-workflow-bench-') as folder:
        root = Path(folder)
        manager = SQLiteManager(root / 'data', root / 'def')
        definition = {'head': {'eng_name':'samples','chn_name':'采样'}, 'columns':[
            dict(eng_name=name, chn_name=name, data_type=kind, primary_key=key, disp_type='文本', disp_menu='')
            for name, kind, key in [('id','int',True),('value','float',False),('name','str',False)]]}
        for name in ['a','b']:
            manager.create_database_file({'name':name, 'tables':[definition]})
            manager.for_database(name+'.sqlite3').insert('samples', {'rows':[
                {'id':i,'value':i/10,'name':f'item_{i}'} for i in range(rows)]})
        a, b = [manager.for_database(name+'.sqlite3') for name in ['a','b']]
        api = SQLiteManagement(a)
        payload = {'sql':'SELECT * FROM samples','compact':True,'limit':500}
        first = api.sql('samples',payload)
        conditional = {**payload, 'if_version':first['result_version'], 'known_records':[r['ref'] for r in first['records']]}
        timings = {}
        for name, request in [('query_500',payload),('unchanged_500',conditional)]:
            values = []
            for _ in range(repeats):
                started=time.perf_counter(); api.sql('samples',request)
                values.append((time.perf_counter()-started)*1000)
            timings[name]={'median_ms':round(statistics.median(values),3), 'p95_ms':round(sorted(values)[int(.95*(len(values)-1))],3)}
        entered = threading.Event()
        def hold():
            with a.session('samples'):
                entered.set(); time.sleep(.15)
        thread=threading.Thread(target=hold); thread.start(); entered.wait(5)
        started=time.perf_counter()
        SQLiteManagement(b).sql('samples',payload)
        timings['other_database_during_150ms_session_ms']=round((time.perf_counter()-started)*1000,3)
        thread.join()
        if hasattr(manager,'close'): manager.close()
        return {'rows_per_database':rows,'databases':2,'repeats':repeats,'scope':'service layer; excludes HTTP and audit fsync','timings':timings}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rows',type=int,default=10000); parser.add_argument('--repeats',type=int,default=30)
    args=parser.parse_args(); result=run(args.rows,args.repeats)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
