"""Bounded background operations with cooperative cancellation at safe boundaries."""
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import threading
import time
import uuid
import re

from .errors import APIError
from .audit import status_for_exception
from .task_store import TaskStore, request_fingerprint

_local = threading.local()
_INTERNAL = ('lock','updated','finished','store','fingerprint','persisted','changed','submitted')


def current_task():
    return getattr(_local, 'task', None)


def record_timing(name, started):
    task=current_task()
    if task:
        with task['lock']:task['timings'][name]=round((time.perf_counter()-started)*1000,3)


def snapshot(task):
    return copy.deepcopy({k:v for k,v in task.items() if k not in _INTERNAL})


def persist(task, force=False):
    task['revision']=task.get('revision',0)+1
    if task.get('changed'):
        with task['changed']: task['changed'].notify_all()
    if task.get('store') and (force or time.monotonic()-task.get('persisted',0)>.5):
        task['store'].put(snapshot(task),task['fingerprint'])
        task['persisted']=time.monotonic()


class TaskCancelled(APIError):
    def __init__(self, completed=()):
        super().__init__(409, '任务已取消；已提交的表保留，未执行部分未写入',
                         details={'cancelled': True, 'completed_tables': list(completed)})


def checkpoint(phase, done=0, total=0):
    task = getattr(_local, 'task', None)
    if task is not None:
        with task['lock']:
            task.update(phase=phase, done=done, total=total)
            persist(task)
            if task['cancel_requested'] and task['cancellable']:
                raise TaskCancelled(task['completed_tables'])


def cancellation_requested():
    task = getattr(_local, 'task', None)
    return bool(task and task['cancel_requested'] and task['cancellable'])


@contextmanager
def committing(phase):
    task = getattr(_local, 'task', None)
    if task is None:
        yield
        return
    with task['lock']:
        checkpoint(phase)
        previous = task['cancellable']
        task['cancellable'] = False
        persist(task,True)
    try:
        yield
    finally:
        with task['lock']: task['cancellable'] = previous


def table_completed(name):
    task = getattr(_local, 'task', None)
    if task is not None:
        with task['lock']:
            task['completed_tables'].append(name)
            persist(task,True)


def background_allowed(method, parts):
    if method == 'GET':
        return parts == ['logs']
    if method == 'POST':
        if parts == ['storage-maintenance']: return True
        if len(parts)==3 and parts[0]=='database-uploads' and parts[2]=='finalize':return True
        if parts == ['database-snapshots'] or (len(parts) == 3 and parts[0] == 'database-snapshots' and parts[2] == 'restore'):
            return True
        return parts in (['definition-preview'], ['definition-import'], ['backup'],
                         ['restore-preview'], ['restore'], ['database-files', 'rebuild-from-embedded']) or (
            len(parts) == 3 and parts[0] == 'tables' and parts[2] in ('sql-export', 'import','rows','clear','sql'))
    return len(parts)==3 and parts[0]=='tables' and (method=='PUT' and parts[2] in ('schema','cell-edits','rows') or method=='DELETE' and parts[2] in ('rows','schema'))


class TaskRegistry:
    def __init__(self, directory=None, manager=None):
        self.lock = threading.RLock()
        self.idle = threading.Condition(self.lock)
        self.queues = OrderedDict()
        self.running_databases = set()
        self.closing = False
        self.tasks = OrderedDict()
        self.store = TaskStore(directory) if directory is not None else None
        try:
            if self.store and manager:
                from .write_receipts import receipt, steps
                for signature, task in self.store.interrupted():
                    if task['path'].startswith('database-uploads/'):
                        try:
                            from .database_uploads import recover_task
                            result=recover_task(manager,task,signature)
                            if result is not None:
                                task.update(status='done',phase='已从上传快照恢复任务结果',result=result)
                                for key in ('error','error_status','details'):task.pop(key,None)
                                self.store.put(task,signature)
                        except Exception:pass
                        continue
                    if not task['database']: continue
                    try:
                        if task['path'].startswith('database-snapshots'):
                            from .database_snapshots import recover_task, scope
                            result = recover_task(scope(manager, task['database']), task, signature)
                        else: result = receipt(manager.for_database(task['database']), task['id'], signature)
                    except Exception: continue
                    if result is not None:
                        task.update(status='done', phase='已从数据库提交回执恢复', result=result)
                        for key in ('error','error_status','details'): task.pop(key,None)
                        self.store.put(task,signature)
                    else:
                        try:completed=steps(manager.for_database(task['database']),task['id'],signature)
                        except Exception:completed=[]
                        if completed:
                            task.update(completed_tables=[item['name'] for item in completed],phase='已核实部分表提交，请核查剩余步骤',
                                        details={'interrupted':True,'completed_tables':[item['name'] for item in completed],'committed_results':completed})
                            self.store.put(task,signature)
            self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='rtdb-task')
        except BaseException:
            if self.store:
                self.store.close()
            raise

    def submit(self, management, method, parts, query, payload, request_id=None):
        if not background_allowed(method, parts):
            raise APIError(400, '该操作不支持后台任务')
        from .storage_maintenance import fence
        with fence(management.manager), self.lock:
            if self.closing: raise APIError(503,'服务正在关闭，请稍后重试')
            ident=request_id or uuid.uuid4().hex
            if not isinstance(ident,str) or not re.fullmatch(r'[a-f0-9]{32}',ident):
                raise APIError(400,'无效的请求编号')
            signature=request_fingerprint(management.manager.database_file,method,parts,query,payload)
            existing=self.tasks.get(ident)
            receipt=self.store.get(ident) if self.store else None
            if existing or receipt:
                previous=existing['fingerprint'] if existing else receipt[0]
                if previous!=signature: raise APIError(409,'该请求编号已用于不同内容，请核查任务；不能重复使用编号')
                return self.get(ident,management.manager.database_file)
            now = time.monotonic()
            for key, task in list(self.tasks.items()):
                if task['status'] in ('done', 'failed', 'cancelled') and now-task['updated'] > 600:
                    del self.tasks[key]
            active=[t for t in self.tasks.values() if t['status'] in ('queued','running')]
            if sum(t['database']==management.manager.database_file for t in active)>=4:
                raise APIError(429, '当前数据库后台任务已满，请稍后重试')
            if len(active)>=16: raise APIError(429,'后台任务总队列已满，请稍后重试')
            while len(self.tasks) >= 8:
                key = next((k for k,t in self.tasks.items() if t['status'] not in ('queued', 'running')), None)
                if key is None: break
                del self.tasks[key]
            task = dict(id=ident, database=management.manager.database_file, method=method,
                        path='/'.join(parts), status='queued', phase='等待执行', done=0, total=0,
                        cancellable=True, cancel_requested=False, completed_tables=[], updated=now,
                        lock=threading.RLock(), finished=threading.Event(),created=time.time(),
                        store=self.store,fingerprint=signature,persisted=0,revision=0,
                        submitted=time.perf_counter(),timings={})
            task['changed']=threading.Condition(task['lock'])
            persist(task,True)
            self.tasks[ident] = task
            self.queues.setdefault(task['database'],deque()).append((task,management,method,parts,query,copy.deepcopy(payload)))
            self._schedule()
            return self.get(ident, task['database'])

    def _schedule(self):
        # Caller holds the registry lock. Waiting work never occupies a worker.
        for database in list(self.queues):
            if len(self.running_databases)>=2: break
            if database in self.running_databases: continue
            queue=self.queues.pop(database); work=queue.popleft()
            if queue:self.queues[database]=queue
            self.running_databases.add(database)
            self.executor.submit(self._execute,*work)

    def _execute(self, task, management, method, parts, query, payload):
        _local.task = task
        started=time.perf_counter()
        try:
            with task['lock']:
                task['status'] = 'running'
                task['timings']['queue_ms']=round((started-task['submitted'])*1000,3)
            checkpoint('准备执行')
            # Imports and log indexing have explicit internal safe points. Other writes
            # are indivisible from the task UI; stopping a client never interrupts commit.
            opaque_write = method != 'GET' and parts not in (['definition-preview'], ['definition-import'],
                ['backup'], ['restore-preview']) and not (len(parts)==3 and parts[2]=='sql-export')
            opaque_write = opaque_write or (parts == ['definition-import'] and not management.manager.database_file)
            if parts and parts[0] == 'database-snapshots': opaque_write = False
            if parts and parts[0] == 'database-uploads': opaque_write = False
            if len(parts)==3 and parts[0]=='tables' and parts[2]=='import' and payload.get('upload_id'): opaque_write=False
            if opaque_write:
                with committing('正在执行并提交；此阶段不可取消'):
                    result = management.dispatch(method, parts, query, payload)
            else:
                result = management.dispatch(method, parts, query, payload)
            with task['lock']:
                task.update(status='done', phase='操作完成' if result.get('ok', True) else '操作完成，部分项目失败', result=result)
        except Exception as error:
            cancelled = isinstance(error, TaskCancelled)
            with task['lock']:
                task.update(status='cancelled' if cancelled else 'failed', phase=str(error),
                            error=str(error), error_status=status_for_exception(error),
                            details=getattr(error, 'details', None))
            if cancelled:
                management.manager.record_operation(category='system', operation='取消后台任务', status='success',
                    http_status=200, method='POST', path='/api/manage/tasks/'+task['id']+'/cancel',
                    result={'completed_tables':task['completed_tables']})
        finally:
            try:
                with task['lock']:
                    task['timings']['execute_ms']=round((time.perf_counter()-started)*1000,3)
                    task['updated'] = time.monotonic(); task['cancellable'] = False
                    try: persist(task,True)
                    except Exception as error:
                        task.update(status='interrupted',error_status=500,phase='任务结果持久化失败',
                            error=f'任务结果未能保存：{error}；操作可能已提交，请核查数据库，勿重复提交')
            finally:
                task['finished'].set()
                with task['changed']:task['changed'].notify_all()
                _local.task = None
                with self.idle:
                    self.running_databases.discard(task['database']);self._schedule();self.idle.notify_all()

    def _find(self, ident, database):
        database = database or None
        with self.lock:
            task = self.tasks.get(ident)
            if task is None and self.store:
                receipt=self.store.get(ident)
                if receipt:
                    task=receipt[1]
                    task.update(lock=threading.RLock(),finished=threading.Event())
                    task['changed']=threading.Condition(task['lock'])
                    task['finished'].set()
            if task is None or (task['database'] or None) != database:
                raise APIError(404, '任务不存在、已过期或服务已重启；请检查操作日志及数据库状态，勿直接重复提交')
            return task

    def get(self, ident, database):
        task = self._find(ident, database)
        with task['lock']:
            return snapshot(task)

    def recent(self):
        with self.lock:
            stored=self.store.recent() if self.store else [snapshot(t) for t in reversed(self.tasks.values())]
            return [{k:v for k,v in task.items() if k!='result'} for task in stored]

    def wait_initial(self, ident, database, timeout=.03):
        self._find(ident, database)['finished'].wait(timeout)
        return self.get(ident, database)

    def wait_change(self, ident, database, revision, timeout=2):
        if not 0<=timeout<=2 or revision<0: raise APIError(400,'无效的任务等待参数')
        task=self._find(ident,database)
        with task['changed']:
            task['changed'].wait_for(lambda:task.get('revision',0)!=revision or task['finished'].is_set(),timeout)
            return snapshot(task)

    def cancel(self, ident, database):
        database = database or None
        task = self._find(ident, database)
        cancelled_work=None
        with self.lock,task['lock']:
            if task['status'] in ('queued','running'):
                if not task['cancellable']: raise APIError(409,'当前正在提交，不能取消；请等待当前阶段结束')
                task['cancel_requested']=True
                queue=self.queues.get(database)
                if queue:
                    cancelled_work=next((work for work in queue if work[0] is task),None)
                    if cancelled_work:
                        queue.remove(cancelled_work)
                        if not queue:del self.queues[database]
                        error=TaskCancelled()
                        task.update(status='cancelled',phase=str(error),error=str(error),error_status=409,
                                    details=error.details,cancellable=False,updated=time.monotonic())
                try:
                    persist(task,True)
                finally:
                    if cancelled_work:
                        task['finished'].set()
                        with task['changed']:
                            task['changed'].notify_all()
                        self.idle.notify_all()
        if cancelled_work:
            cancelled_work[1].manager.record_operation(category='system',operation='取消排队任务',status='success',
                http_status=200,method='POST',path='/api/manage/tasks/'+ident+'/cancel',result={'cancelled':True})
        return self.get(ident, database)

    def close(self):
        with self.idle:
            self.closing=True
            # 关服时主动取消尚未开始执行的排队任务，避免长时间阻塞关闭。
            while self.queues:
                _db, queue = self.queues.popitem(last=False)
                while queue:
                    task = queue.popleft()[0]
                    with task['lock']:
                        error = TaskCancelled()
                        task.update(status='cancelled', phase='服务关闭，排队任务已取消', error=str(error),
                                    error_status=409, details=error.details, cancellable=False, updated=time.monotonic())
                        try:
                            persist(task, True)
                        except Exception:
                            pass
                        task['finished'].set()
                        with task['changed']:
                            task['changed'].notify_all()
            self.idle.notify_all()
            self.idle.wait_for(lambda:not self.running_databases and not self.queues, timeout=30)
        self.executor.shutdown(wait=True)
        if self.store: self.store.close()
