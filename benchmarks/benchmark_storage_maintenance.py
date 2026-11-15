"""Measure metadata-only previews and task-detail compaction in disposable data."""
from contextlib import closing
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager
from web.task_store import TaskStore


def seed(manager, count=32, file_bytes=8*1024**2):
    root=manager.data_dir/'.web-snapshots';root.mkdir(exist_ok=True)
    old=time.time()-40*86400
    for _ in range(count):
        ident=uuid.uuid4().hex
        path=root/(ident+'.sqlite3')
        with path.open('wb') as stream:stream.truncate(file_bytes)
        manifest=path.with_suffix('.json')
        manifest.write_text(json.dumps(dict(id=ident,filename='benchmark_'+ident[:8]+'.sqlite3',bytes=file_bytes)),encoding='utf-8')
        for item in (path,manifest):os.utime(item,(old,old))
    store=TaskStore(manager.data_dir/'.web-tasks')
    try:
        for _ in range(100):
            ident=uuid.uuid4().hex
            store.put(dict(id=ident,database=None,method='POST',path='backup',created=old,status='done',
                           phase='完成',completed_tables=[],timings={},result={'text':'结果'*20000}),ident)
        with closing(store.connect()) as db,db:db.execute('UPDATE tasks SET updated=?',(old,))
    finally:store.close()


def paired_cleanup():
    timings={'per_task_transaction':[],'bounded_batch_transaction':[]}
    for round_number in range(3):
        order=list(timings)
        if round_number%2:order.reverse()
        for variant in order:
            with tempfile.TemporaryDirectory(prefix='rtdb-storage-pair-') as temporary:
                manager=SQLiteManager(Path(temporary)/'data',Path(temporary)/'definitions')
                try:
                    seed(manager)
                    maintenance=manager.storage_maintenance
                    if variant=='per_task_transaction':
                        compact=maintenance._compact_tasks
                        maintenance._compact_tasks=lambda items:[compact([item]) for item in items]
                    preview=maintenance.preview()
                    started=time.perf_counter();result=maintenance.clean({'token':preview['token']})
                    timings[variant].append((time.perf_counter()-started)*1000)
                    assert result['ok'] and result['compacted_tasks']==100 and result['cleaned']==132
                finally:manager.close()
    before=statistics.median(timings['per_task_transaction'])
    after=statistics.median(timings['bounded_batch_transaction'])
    return dict(method='three paired rounds, alternating order, identical fixture; local new-feature transaction comparison',
                samples_ms=timings,before_median_ms=before,after_median_ms=after,speedup=before/after)


def main():
    with tempfile.TemporaryDirectory(prefix='rtdb-storage-bench-') as temporary:
        manager=SQLiteManager(Path(temporary)/'data',Path(temporary)/'definitions')
        try:
            seed(manager)
            maintenance=manager.storage_maintenance
            before=maintenance.preview();samples=[]
            for _ in range(20):
                started=time.perf_counter();maintenance.preview();samples.append((time.perf_counter()-started)*1000)
            tracemalloc.start();preview=maintenance.preview();_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
            started=time.perf_counter();cleaned=maintenance.clean({'token':preview['token']});clean_ms=(time.perf_counter()-started)*1000
            after=maintenance.preview()
            result=dict(fixture='synthetic service artifacts; 32 sparse 8 MiB files and 100 task results; no business data',
                        samples=20,preview_p50_ms=statistics.median(samples),preview_p95_ms=sorted(samples)[18],
                        python_peak_bytes=peak,cleanup_ms=clean_ms,cleanup=cleaned,
                        before=before['statistics'],after=after['statistics'])
            result['transaction_comparison']=paired_cleanup()
            output=Path(__file__).resolve().parents[1]/'output/storage-maintenance-benchmark.json'
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False,indent=2))
        finally:manager.close()


if __name__=='__main__':main()
