"""Disposable storage-maintenance browser fixture; never opens production data."""
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager
from web.sqlite_server import create_sqlite_server
from web.test_sqlite_multi_database import table_definition
from benchmarks.benchmark_storage_maintenance import seed

root=Path(__file__).resolve().parents[1]/'output/playwright'/('storage-lab-'+uuid.uuid4().hex[:8])
manager=SQLiteManager(root/'sqlite_dat',root/'def/table')
manager.create_database_file({'name':'storage_lab','tables':[table_definition('nodes','测试表','id')]})
manager.for_database('storage_lab.db').insert('nodes',{'rows':[{'id':1,'name':'业务数据应保留'}]})
seed(manager, count=3, file_bytes=65536)
manager.close()
server=create_sqlite_server('127.0.0.1',0,root=root)
info=dict(pid=os.getpid(),url=f'http://127.0.0.1:{server.server_port}',root=str(root))
(root.parent/'storage-server.json').write_text(json.dumps(info),encoding='utf-8')
print(json.dumps(info),flush=True)
try:server.serve_forever()
finally:server.server_close()
