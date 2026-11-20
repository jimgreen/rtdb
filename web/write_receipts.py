"""Single-database write receipts and audit outbox committed with business data."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
import json
import sqlite3
import time
import threading
import hashlib
import uuid

from .errors import APIError
from .audit import _local_now, result_summary

_current = ContextVar('web_write_operation', default=None)
TABLE = 'rtdb_web_commits'


@contextmanager
def operation(manager, method, parts, query, payload, descriptor):
    from .tasks import current_task
    from .task_store import request_fingerprint
    task = current_task()
    category, title, table = descriptor
    context = dict(manager=manager, id=task['id'] if task else uuid.uuid4().hex,
                   fingerprint=task['fingerprint'] if task else request_fingerprint(manager.database_file,method,parts,query,payload),
                   committed=False, started=time.perf_counter(), category=category, operation=title,
                   enabled=method!='GET' and len(parts)==3 and parts[0]=='tables' and parts[2] in ('rows','cell-edits','clear','import','sql','schema'),
                   parts=parts,
                   method=method, path='/api/manage/'+'/'.join(parts), table=table)
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def write(table, callback):
    started=time.perf_counter()
    result=table._write(lambda connection: in_transaction(connection,callback))
    from .tasks import record_timing
    record_timing('transaction_ms',started)
    mark_committed(result)
    return result


def mark_committed(result):
    context = _current.get()
    if context and context['enabled'] and context['manager'].database_file:
        context.update(committed=True,result=result)


def committed_result():
    context=_current.get()
    return context.get('result') if context and context['committed'] else None


def in_transaction(connection, callback):
    """Caller owns BEGIN/COMMIT, including all DDL and the durable receipt."""
    context=_current.get()
    if not context or not context['enabled'] or not context['manager'].database_file:return callback(connection)
    connection.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL, event TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)')
    connection.execute(f'CREATE INDEX IF NOT EXISTS rtdb_web_pending ON {TABLE}(delivered) WHERE delivered=0')
    existing=connection.execute(f'SELECT fingerprint,result FROM {TABLE} WHERE id=?',(context['id'],)).fetchone()
    if existing:
        if existing[0]!=context['fingerprint']:raise APIError(409,'请求编号已用于不同的写入内容')
        from .receipt_maintenance import decode_result
        return decode_result(connection,context['id'],existing[1])
    result=callback(connection)
    started=time.perf_counter()
    event=dict(id=context['id'],request_id=context['id'],timestamp=_local_now().isoformat(timespec='milliseconds'),
               database=context['manager'].database_file,category=context['category'],operation=context['operation'],
               status='success',http_status=200,method=context['method'],path=context['path'],table=context['table'],
               duration_ms=round((time.perf_counter()-context['started'])*1000,2),result=result_summary(result),error=None)
    connection.execute(f'INSERT INTO {TABLE}(id,fingerprint,result,event) VALUES(?,?,?,?)',
                       (context['id'],context['fingerprint'],json.dumps(result,ensure_ascii=False,allow_nan=False),json.dumps(event,ensure_ascii=False,allow_nan=False)))
    from .tasks import record_timing
    record_timing('receipt_ms',started)
    return result


def record_step(connection, name, result):
    context=_current.get()
    if not context or not context['manager'].database_file or context['parts'] not in (['definition-import'],['restore']):return
    connection.execute('CREATE TABLE IF NOT EXISTS rtdb_web_steps (request_id TEXT NOT NULL, name TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(request_id,name))')
    connection.execute('INSERT INTO rtdb_web_steps VALUES(?,?,?,?)',
                       (context['id'],name,context['fingerprint'],json.dumps(result,ensure_ascii=False,allow_nan=False)))
    child={**context,'id':hashlib.sha256((context['id']+':'+name).encode()).hexdigest()[:32],
           'enabled':True,'table':name,'operation':context['operation']+'：提交表 '+name}
    token=_current.set(child)
    try:in_transaction(connection,lambda _:result)
    finally:_current.reset(token)
    context['steps_written']=True


def steps(manager, ident, signature):
    with manager.read_scope(), closing(sqlite3.connect((manager.data_dir/manager.database_file).as_uri()+'?mode=ro',uri=True,timeout=5)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='rtdb_web_steps' AND type='table'").fetchone():return []
        return [json.loads(row[0]) for row in connection.execute('SELECT result FROM rtdb_web_steps WHERE request_id=? AND fingerprint=? ORDER BY rowid',(ident,signature))]


def receipt(manager, ident, signature):
    with manager.read_scope(), closing(sqlite3.connect((manager.data_dir/manager.database_file).as_uri()+'?mode=ro',uri=True,timeout=5)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():
            return None
        row = c.execute(f'SELECT fingerprint,result FROM {TABLE} WHERE id=?',(ident,)).fetchone()
        if not row: return None
        if row[0] != signature: raise APIError(409,'数据库回执与任务请求不一致，请核查')
        from .receipt_maintenance import decode_result
        return decode_result(c,ident,row[1])


def publish(manager, ident=None, *, recovery=False, batch_size=200, max_bytes=2*1024*1024):
    """Never turn a committed operation into a reported failure of the data write."""
    try:
        if manager.read_only:
            return True
        with manager.read_scope(), closing(sqlite3.connect((manager.data_dir/manager.database_file).as_uri()+'?mode=rw',uri=True,timeout=5)) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():
                return True
            cursor=c.execute(f'SELECT id,event FROM {TABLE} WHERE delivered=0'+(' AND id=?' if ident else '')+' ORDER BY rowid LIMIT ?', ((ident,batch_size) if ident else (batch_size,)))
            rows=[];size=0
            for row in cursor:
                count=len(row[1].encode('utf-8'))
                if rows and size+count>max_bytes:break
                rows.append(row);size+=count
            cursor.close()
            if len(rows)==1:
                logged=manager.audit_log.write_event(json.loads(rows[0][1]), recovery=recovery) is not None
            else:
                logged=manager.audit_log.write_events([json.loads(encoded) for _,encoded in rows], recovery=recovery)
            if not logged: return False
            with c:
                c.executemany(f'UPDATE {TABLE} SET delivered=1 WHERE id=?', [(ident,) for ident,_ in rows])
        return True
    except Exception:
        return False


def pending(manager):
    if manager.read_only:
        return False
    with manager.read_scope(),closing(sqlite3.connect((manager.data_dir/manager.database_file).as_uri()+'?mode=ro',uri=True,timeout=5)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone():return False
        return bool(c.execute(f'SELECT 1 FROM {TABLE} WHERE delivered=0 LIMIT 1').fetchone())


def _receipt_databases(manager):
    from .sqlite_service import is_listed_database
    for path in manager.data_dir.iterdir():
        if path.resolve() == path and is_listed_database(path):
            yield path.name


def recover_pending(manager):
    for filename in _receipt_databases(manager):
        try:
            scoped=manager.for_database(filename)
            while publish(scoped,recovery=True) and pending(scoped):pass
        except Exception: pass  # Inaccessible databases remain available for the repair UI.


class AuditDelivery:
    """Bounded, coalescing notification queue. The durable queue lives in SQLite."""
    def __init__(self, manager):
        self.manager=manager; self.condition=threading.Condition(); self.pending={}
        self.active=None; self.stopping=False; self.error=None
        self.retry_at={};self.errors={};self.archive_queue={};self.archive_active=None;self.archive_due={};self.archive_errors={}
        self.pending={filename:True for filename in _receipt_databases(manager)}
        self.thread=threading.Thread(target=self._run,name='rtdb-audit-delivery',daemon=True)
        self.thread.start()

    def enqueue(self, filename, recovery=False):
        with self.condition:
            self.pending[filename]=self.pending.get(filename,False) or recovery
            self.condition.notify_all()

    def request_archive(self,filename):
        with self.condition:
            self.archive_queue[filename]=True;self.condition.notify_all()

    def status(self):
        with self.condition:
            return dict(pending_databases=sorted(set(self.pending)|({self.active} if self.active else set())),
                        error='；'.join(f'{name}：{error}' for name,error in self.errors.items()) or None,
                        archive_databases=sorted(set(self.archive_queue)|({self.archive_active} if self.archive_active else set())),
                        archive_errors=dict(self.archive_errors),batch_limit=200,batch_bytes=2*1024*1024)

    def drain(self, timeout=5):
        deadline=time.monotonic()+timeout
        with self.condition:
            self.condition.notify_all()
            while self.pending or self.active:
                remaining=deadline-time.monotonic()
                if remaining<=0:return False
                self.condition.wait(remaining)
            return True

    def _run(self):
        while True:
            with self.condition:
                while True:
                    if self.stopping:return
                    now=time.monotonic()
                    for name,due in self.archive_due.items():
                        if due<=now:self.archive_queue.setdefault(name,True)
                    filename=next((name for name in self.pending if self.retry_at.get(name,0)<=now),None)
                    archival=filename is None
                    if archival:filename=next(iter(self.archive_queue),None)
                    if filename is not None:break
                    self.condition.wait(1 if self.pending else 60)
                if archival:
                    self.archive_queue.pop(filename);self.archive_active=filename
                else:
                    self.condition.wait(.05)
                    if self.stopping:return
                    recovery=self.pending.pop(filename);self.active=filename
            more=False;archive_warning=None
            try:
                scoped=self.manager.for_database(filename)
                if archival:
                    from .receipt_maintenance import archive_batch
                    result=archive_batch(scoped);ok=True;more=result['more']
                    if result['archived'] and not scoped.record_operation(category='system',operation='压缩历史回执',status='success',
                            http_status=200,method='SYSTEM',path='/receipt-maintenance',result=result):
                        archive_warning='回执压缩已完成，维护日志写入失败'
                else:
                    ok=publish(scoped,recovery=recovery)
                    more=ok and pending(scoped)
            except Exception as error:
                ok=False
                if archival:
                    archive_warning=f'回执压缩失败，将自动重试：{error}'
                    try:self.manager.record_operation(category='system',operation='压缩历史回执',status='failure',http_status=500,
                        method='SYSTEM',path='/receipt-maintenance',result={'database':filename},error=error)
                    except Exception:pass
            with self.condition:
                if archival:
                    self.archive_active=None;self.archive_due[filename]=time.monotonic()+3600
                    if ok:
                        self.archive_errors.pop(filename,None)
                        if more:self.archive_queue.setdefault(filename,True)
                    else:self.archive_errors[filename]='回执压缩失败，将自动重试'
                    if archive_warning:self.archive_errors[filename]=archive_warning
                else:
                    self.active=None
                    if not ok:
                        self.pending[filename]=True;self.retry_at[filename]=time.monotonic()+2
                        self.errors[filename]='日志同步失败，审计事件保留，将自动重试'
                    else:
                        self.errors.pop(filename,None);self.retry_at.pop(filename,None)
                        if more:self.pending[filename]=self.pending.get(filename,False) or recovery
                        if filename not in self.archive_due:self.archive_due[filename]=0
                self.condition.notify_all()

    def close(self):
        self.drain(3)
        with self.condition:self.stopping=True;self.condition.notify_all()
        self.thread.join(10)
