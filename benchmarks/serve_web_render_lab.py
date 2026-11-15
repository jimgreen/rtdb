"""Serve isolated 600 x 40 browser fixtures; never use a business database directory."""
import argparse
import json
import os
from pathlib import Path
import sys

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
from web.sqlite_server import create_sqlite_server
from web.sqlite_service import SQLiteManager


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-logs',action='store_true',help='Include 100,000 fixture logs for progress/cancellation checks')
    args=parser.parse_args()
    root=repo/'output/browser-render-lab'
    manager=SQLiteManager(root/'sqlite_dat',root/'def/table')
    try:
        if not (root/'sqlite_dat/lab.sqlite3').exists():
            columns=[dict(eng_name='id',chn_name='编号',data_type='int',primary_key=True,disp_type='整数',disp_menu='')]
            columns += [dict(eng_name=f'v{i:02}',chn_name=f'测量值{i:02}',data_type='float',primary_key=False,disp_type='浮点数',disp_menu='') for i in range(1,40)]
            manager.create_database_file({'name':'lab','tables':[{'head':{'eng_name':'samples','chn_name':'宽表'},'columns':columns}]})
            manager.for_database('lab.sqlite3').insert('samples',{'rows':[
                {'id':i,**{f'v{j:02}':i+j/100 for j in range(1,40)}} for i in range(1,601)]})
        if args.task_logs:
            path=manager.audit_log.log_dir/'2026-11-13.jsonl'
            if not path.exists():
                with path.open('w',encoding='utf-8') as stream:
                    for i in range(100000):
                        stream.write(json.dumps(dict(id=f'fixture-{i}',timestamp='2026-11-13T12:00:00+13:00',category='data',operation='任务验证',status='success',method='GET',path='/fixture'),ensure_ascii=False)+'\n')
    finally:
        manager.close()
    server=create_sqlite_server('127.0.0.1',0,root=root)
    state={'pid':os.getpid(),'url':f'http://127.0.0.1:{server.server_port}'}
    (root/'server.json').write_text(json.dumps(state),encoding='utf-8')
    print(json.dumps(state),flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=='__main__': main()
