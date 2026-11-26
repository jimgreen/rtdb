"""Create a self-contained >16 MiB upload fixture without touching business files."""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager
from web.test_sqlite_multi_database import table_definition
from web import database_snapshots

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='rtdb-upload-fixture-') as temp:
    manager=SQLiteManager(Path(temp)/'data',Path(temp)/'defs')
    try:
        definition=table_definition('nodes','节点','id');definition['columns'][1]['default_value']='默认名称'
        manager.create_database_file({'name':'upload_source','tables':[definition,table_definition('lines','线路','id')]})
        scoped=manager.for_database('upload_source.db')
        scoped.insert('nodes',{'rows':[{'id':i,'name':'上传节点'+str(i)} for i in range(250)]})
        with closing(sqlite3.connect(scoped.data_dir/scoped.database_file)) as connection,connection:
            connection.execute('CREATE TABLE attachment(data BLOB)')
            connection.execute('INSERT INTO attachment VALUES(zeroblob(?))',(20*1024**2,))
        item=database_snapshots.create(scoped);source,_=database_snapshots.resolve(scoped,item['id'])
        target=root/'output/playwright/upload-large.db';target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target);print(str(target),target.stat().st_size)
    finally:manager.close()
