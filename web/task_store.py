"""Durable task receipts, separate from business schemas and audit JSONL."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .errors import APIError
from .sqlite_runtime import file_fence


def request_fingerprint(database, method, parts, query, payload):
    value=json.dumps([database,method,parts,query,payload],ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class TaskStore:
    def __init__(self, directory):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'ledger.sqlite'
        # A second executor cannot reinterpret a live server's tasks as interrupted.
        self.ownership=file_fence(self.directory/'executor.lock',True,timeout=.1)
        self.ownership.__enter__()
        try:
            with closing(self.connect()) as c, c:
                c.execute('PRAGMA journal_mode=WAL')
                c.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, database_name TEXT, created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL, state TEXT NOT NULL, result TEXT)')
                c.execute('CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created DESC)')
                c.execute('CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status)')
                c.execute('CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated)')
                self.expire_results(c)
                for ident,encoded in c.execute("SELECT id,state FROM tasks WHERE status IN ('queued','running')").fetchall():
                    state=json.loads(encoded)
                    if state['status'] in ('queued','running'):
                        state.update(status='interrupted',cancellable=False,error_status=409,
                            error='服务曾中断，任务未自动重执行；提交结果可能尚未确认，请核查操作日志和数据库',
                            phase='服务中断，需核查结果',details={'interrupted':True,'completed_tables':state.get('completed_tables',[])})
                        c.execute("UPDATE tasks SET status='interrupted',state=?,updated=? WHERE id=?",(json.dumps(state,ensure_ascii=False),time.time(),ident))
            self.next_cleanup=time.time()+3600
        except BaseException:
            self.ownership.__exit__(None,None,None)
            raise

    def connect(self):
        c=sqlite3.connect(self.path,timeout=5)
        c.execute('PRAGMA synchronous=FULL')
        return c

    def expire_results(self, connection):
        from types import SimpleNamespace
        from .storage_maintenance import configuration
        try:
            days = configuration(SimpleNamespace(data_dir=self.directory.parent))['task_retention_days']
        except APIError:
            return  # Invalid policy must never cause automatic data removal.
        if days:
            connection.execute("UPDATE tasks SET result=NULL WHERE updated<? AND status IN ('done','failed','cancelled')",
                               (time.time()-days*86400,))

    def get(self, ident):
        with closing(self.connect()) as c:
            row=c.execute('SELECT fingerprint,state,result FROM tasks WHERE id=?',(ident,)).fetchone()
        if not row: return None
        state=json.loads(row[1])
        if row[2] is not None: state['result']=json.loads(row[2])
        elif state['status']=='done': state['result_expired']=True
        return row[0],state

    def put(self, state, fingerprint):
        encoded=json.dumps({k:v for k,v in state.items() if k!='result'},ensure_ascii=False,allow_nan=False)
        result=json.dumps(state['result'],ensure_ascii=False,allow_nan=False) if 'result' in state else None
        with closing(self.connect()) as c, c:
            if time.time()>self.next_cleanup:
                self.expire_results(c)
                self.next_cleanup=time.time()+3600
            c.execute('INSERT INTO tasks(id,fingerprint,database_name,created,updated,status,state,result) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,status=excluded.status,state=excluded.state,result=excluded.result',
                (state['id'],fingerprint,state['database'],state['created'],time.time(),state['status'],encoded,result))

    def recent(self, limit=30):
        with closing(self.connect()) as c:
            rows=c.execute('SELECT state,result IS NOT NULL FROM tasks ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        return [dict(json.loads(row[0]),result_available=bool(row[1])) for row in rows]

    def interrupted(self):
        with closing(self.connect()) as c:
            return [(row[0], json.loads(row[1])) for row in c.execute("SELECT fingerprint,state FROM tasks WHERE status='interrupted'")]

    def close(self):
        self.ownership.__exit__(None,None,None)
