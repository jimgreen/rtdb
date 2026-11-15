"""Compare OFFSET with cursor reads on an isolated million-row SQLite table."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo))
from web.sqlite_service import SQLiteManager
from web.management import SQLiteManagement


def summary(values):
    ordered=sorted(values)
    return {'median_ms':round(statistics.median(ordered),3),'p95_ms':round(ordered[int(.95*(len(ordered)-1))],3)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows',type=int,default=1000000)
    parser.add_argument('--repeats',type=int,default=15)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='rtdb-paging-') as root:
        root=Path(root);manager=SQLiteManager(root/'data',root/'definitions')
        try:
            columns=[dict(eng_name=name,chn_name=name,data_type=kind,primary_key=name=='id',disp_type='文本',disp_menu='') for name,kind in [('id','int'),('name','str'),('value','float')]]
            manager.create_database_file({'name':'lab','tables':[{'head':{'eng_name':'samples','chn_name':'样本'},'columns':columns}]})
            scoped=manager.for_database('lab.sqlite3')
            with scoped.session('samples') as table:
                table._write(lambda c:c.executemany(f'INSERT INTO {table._data_sql}(c0,c1,c2) VALUES(?,?,?)',((i,f'item-{i}',i/10) for i in range(1,args.rows+1))))
            api=SQLiteManagement(scoped);data=[];limit=100
            for offset in [100,10000,args.rows//2,args.rows-1000]:
                query={'sql':'SELECT * FROM samples','compact':True,'limit':limit,'offset':offset}
                anchor=api.sql('samples',{**query,'offset':offset-limit})['next_cursor']
                samples={'offset':[],'cursor':[]};query_cost={'offset':[],'cursor':[]}
                for iteration in range(-2,args.repeats):
                    # Alternate order to reduce systematic warm-cache bias.
                    for mode in (['offset','cursor'] if iteration%2 else ['cursor','offset']):
                        started=time.perf_counter()
                        result=api.sql('samples',{**query,**({'page_cursor':anchor} if mode=='cursor' else {})})
                        elapsed=(time.perf_counter()-started)*1000
                        assert [r['row']['id'] for r in result['records']]==list(range(offset+1,offset+101))
                        assert result['pagination_mode']==mode
                        if iteration>=0:samples[mode].append(elapsed);query_cost[mode].append(result['timings']['query_ms'])
                data.append({'offset':offset,'offset_read':summary(samples['offset']),'cursor_read':summary(samples['cursor']),
                    'sql_fetch_ms':{k:summary(v) for k,v in query_cost.items()},'samples_ms':samples})
                print(json.dumps(data[-1],ensure_ascii=False),flush=True)
            report={'rows':args.rows,'columns':3,'page_size':limit,'repeats':args.repeats,'warmups':2,
                'scope':'SQLiteManagement.sql: includes open, SQL/fetch, decode, versions, cursor bookkeeping; excludes HTTP and audit dispatch','results':data}
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:manager.close()


if __name__=='__main__':main()
