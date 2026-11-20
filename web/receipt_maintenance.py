"""Lossless, bounded receipt compression inside the portable business database."""
from contextlib import closing
import json
import sqlite3
import time
import zlib

from .errors import APIError

TABLE='rtdb_web_commits'
ARCHIVE='rtdb_web_receipt_archive'
CONFIG='rtdb_web_receipt_config'
DEFAULT_DAYS=7
MAX_BATCH_BYTES=2*1024*1024


def exists(connection,name):
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())


def connect(manager, *, readonly=False):
    if not manager.database_file:raise APIError(400,'请先选择数据库')
    path=manager.data_dir/manager.database_file
    if not path.is_file():raise APIError(404,'数据库文件不存在')
    connection=sqlite3.connect(path.as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=5)
    if not readonly:
        connection.execute('PRAGMA synchronous=FULL')
    return connection


def _days(connection):
    row=connection.execute(f'SELECT days FROM {CONFIG} WHERE id=1').fetchone() if exists(connection,CONFIG) else None
    return row[0] if row else DEFAULT_DAYS


def configure(manager,payload):
    manager.require_writable()
    days=payload.get('days')
    if isinstance(days,bool) or not isinstance(days,int) or not 0<=days<=36500:
        raise APIError(422,'压缩天数须为 0 至 36500 的整数，0 表示关闭自动压缩')
    with manager.read_scope(),closing(connect(manager)) as c,c:
        c.execute(f'CREATE TABLE IF NOT EXISTS {CONFIG} (id INTEGER PRIMARY KEY CHECK(id=1), days INTEGER NOT NULL)')
        c.execute(f'INSERT INTO {CONFIG} VALUES(1,?) ON CONFLICT(id) DO UPDATE SET days=excluded.days',(days,))
    return {'days':days}


def decode_result(connection,ident,encoded):
    if encoded!='null':return json.loads(encoded)
    # A tombstone must never be treated as permission to execute an old write.
    if not exists(connection,ARCHIVE):raise APIError(409,'归档回执缺失，不能重复执行，请核查数据库')
    row=connection.execute(f'SELECT body,digest FROM {ARCHIVE} WHERE id=?',(ident,)).fetchone()
    if not row:raise APIError(409,'归档回执缺失，不能重复执行，请核查数据库')
    import hashlib
    raw=zlib.decompress(row[0])
    if hashlib.sha256(raw).hexdigest()!=row[1]:raise APIError(409,'归档回执校验失败，不能重复执行')
    return json.loads(raw)['result']


def archive_batch(manager,*,batch_size=200,max_bytes=MAX_BATCH_BYTES):
    """Compress delivered, aged receipts atomically; preserve identifiers and results."""
    import hashlib
    if manager.read_only:
        return {'archived':0,'more':False,'days':0}
    with manager.read_scope(),closing(connect(manager)) as c,c:
        days=_days(c)
        if not days or not exists(c,TABLE):return {'archived':0,'more':False,'days':days}
        c.execute('BEGIN IMMEDIATE')
        c.execute(f'CREATE TABLE IF NOT EXISTS {ARCHIVE} (id TEXT PRIMARY KEY, body BLOB NOT NULL, digest TEXT NOT NULL, original_bytes INTEGER NOT NULL)')
        c.execute(f"CREATE INDEX IF NOT EXISTS rtdb_web_archive_due ON {TABLE}(unixepoch(json_extract(event,'$.timestamp'))) WHERE delivered=1 AND result<>'null'")
        cutoff=time.time()-days*86400
        cursor=c.execute(f"SELECT id,result,event FROM {TABLE} WHERE delivered=1 AND result<>'null' AND unixepoch(json_extract(event,'$.timestamp'))<? ORDER BY unixepoch(json_extract(event,'$.timestamp')) LIMIT ?",(cutoff,batch_size+1))
        batch=[];size=0;more=False
        for ident,result,event in cursor:
            if len(batch)>=batch_size or batch and size+len(result.encode())+len(event.encode())>max_bytes:
                more=True;break
            raw=json.dumps({'result':json.loads(result),'event':json.loads(event)},ensure_ascii=False,separators=(',',':')).encode()
            batch.append((ident,zlib.compress(raw),hashlib.sha256(raw).hexdigest(),len(raw)))
            size+=len(raw)
        cursor.close()
        c.executemany(f'INSERT INTO {ARCHIVE} VALUES(?,?,?,?)',batch)
        c.executemany(f"UPDATE {TABLE} SET result='null',event='{{}}' WHERE id=?",[(row[0],) for row in batch])
        return {'archived':len(batch),'more':more,'days':days,'source_bytes':size}


def statistics(manager):
    with manager.read_scope(),closing(connect(manager,readonly=True)) as c:
        result={'days':_days(c),'pending':0,'delivered':0,'archived':0,'original_bytes':0,'compressed_bytes':0,'oldest_pending':None}
        if exists(c,TABLE):
            result['pending']=c.execute(f'SELECT COUNT(*) FROM {TABLE} WHERE delivered=0').fetchone()[0]
            result['delivered']=c.execute(f'SELECT COUNT(*) FROM {TABLE} WHERE delivered=1').fetchone()[0]
            result['oldest_pending']=c.execute(f"SELECT MIN(json_extract(event,'$.timestamp')) FROM {TABLE} WHERE delivered=0").fetchone()[0]
        if exists(c,ARCHIVE):
            result['archived'],result['original_bytes'],result['compressed_bytes']=c.execute(f'SELECT COUNT(*),COALESCE(SUM(original_bytes),0),COALESCE(SUM(length(body)),0) FROM {ARCHIVE}').fetchone()
        return result
