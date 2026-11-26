"""Paired JSON table-backup vs complete SQLite snapshot, isolated real files."""
import argparse
from contextlib import closing
from datetime import datetime
import gc
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager
from web.management import SQLiteManagement
from web.database_snapshots import create, resolve, restore
from web.test_sqlite_multi_database import table_definition


def summary(samples):
    values=sorted(samples)
    return dict(median_ms=statistics.median(values), min_ms=min(values), max_ms=max(values), samples_ms=samples)


def measure(count, repeats):
    with tempfile.TemporaryDirectory(prefix='rtdb-snapshot-benchmark-') as temp:
        root=Path(temp);manager=SQLiteManager(root/'data',root/'defs')
        manager.create_database_file({'name':'lab','tables':[table_definition('nodes','节点','id'),table_definition('lines','线路','id')]})
        scoped=manager.for_database('lab.db');api=SQLiteManagement(scoped)
        try:
            for name in ('nodes','lines'):
                for start in range(0,count,2000):
                    scoped.insert(name,{'rows':[{'id':i,'name':'测点_'+str(i)+'_'+'x'*160} for i in range(start,min(start+2000,count))]})
            def old():
                value=api.backup({'tables':['nodes','lines']})['text']
                with (root/'backup.json').open('w',encoding='utf-8',newline='') as stream:
                    stream.write(value);stream.flush();os.fsync(stream.fileno())
                return (root/'backup.json').stat().st_size
            def new(): return create(scoped)
            old();new()  # One warmup per method.
            before=[];after=[];last=None;old_bytes=0
            for repeat in range(repeats):
                for method in (['old','new'] if repeat%2==0 else ['new','old']):
                    gc.collect();started=time.perf_counter()
                    value=old() if method=='old' else new()
                    (before if method=='old' else after).append((time.perf_counter()-started)*1000)
                    if method=='old':old_bytes=value
                    else:last=value
            memory={}
            # Separate memory pass: tracing overhead is excluded from timing samples.
            for name,callback in [('json',old),('sqlite',new)]:
                gc.collect();tracemalloc.start();callback();memory[name]=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
            path,_=resolve(scoped,last['id'])
            with closing(sqlite3.connect(path)) as connection:
                assert connection.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                for name in ('nodes','lines'):
                    assert connection.execute(f'SELECT COUNT(*) FROM rtdb_data_{name}').fetchone()[0]==count
            started=time.perf_counter();restored=restore(scoped,last['id'],{'name':'restored'})
            restore_ms=(time.perf_counter()-started)*1000
            with closing(sqlite3.connect(root/'data'/restored['filename'])) as connection:
                assert connection.execute('SELECT COUNT(*) FROM rtdb_data_nodes').fetchone()[0]==count
            return dict(rows_per_table=count,tables=2,total_rows=count*2,json=summary(before),sqlite=summary(after),
                        python_peak_bytes=memory,json_bytes=old_bytes,sqlite_bytes=last['bytes'],restore_ms=restore_ms,verified=True)
        finally:manager.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('output/database-snapshot-performance-20261115.json'))
    parser.add_argument('--repeats',type=int,default=5);args=parser.parse_args()
    result=dict(created=datetime.now().astimezone().isoformat(),python=platform.python_version(),sqlite=sqlite3.sqlite_version,
                platform=platform.platform(),repeats=args.repeats,results=[])
    for count in (1000,10000,50000):
        row=measure(count,args.repeats);result['results'].append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
