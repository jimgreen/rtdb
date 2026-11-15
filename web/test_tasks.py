"""Real-file and HTTP regressions for progress, cancellation and task isolation."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import urlopen

from web import test_database_scope, test_sqlite_server
from web.errors import APIError
from web.management import SQLiteManagement
from web.definition_import import DefinitionImporter
from web.tasks import TaskRegistry, checkpoint, committing, TaskCancelled
from core import SchemaError


class TaskTests(unittest.TestCase):
    def setUp(self):
        test_database_scope.DatabaseScopeTests.setUp(self)
        self.registry = TaskRegistry()
        self.scoped = self.manager.for_database('plant_a.sqlite3')
        self.api = SQLiteManagement(self.scoped)
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        self.registry.close()
        test_database_scope.DatabaseScopeTests.tearDown(self)

    def submit(self, action='definition-preview', payload=None, api=None):
        return self.registry.submit(api or self.api, 'POST', [action], {}, payload or {})

    def finished(self, task):
        result = self.registry.wait_initial(task['id'], task['database'], 5)
        self.assertIn(result['status'], ('done', 'failed', 'cancelled'), result)
        return result

    def fake(self, dispatch):
        return SimpleNamespace(manager=self.scoped, dispatch=dispatch)

    def test_completion_result_and_database_isolation(self):
        task = self.submit(api=self.fake(lambda *args: {'ok': True, 'value': 7}))
        self.assertEqual(self.finished(task)['result']['value'], 7)
        with self.assertRaises(APIError) as caught:
            self.registry.get(task['id'], 'plant_b.sqlite3')
        self.assertEqual(caught.exception.status, 404)

    def test_reject_unknown_background_operation(self):
        with self.assertRaises(APIError): self.submit('arbitrary-delete')

    def test_failure_preserves_message_and_http_status(self):
        def fail(*args): raise APIError(422, '具体校验失败', details={'row': 2})
        result = self.finished(self.submit(api=self.fake(fail)))
        self.assertEqual((result['status'], result['error_status'], result['details']), ('failed', 422, {'row': 2}))

    def test_schema_error_keeps_sync_http_classification(self):
        def fail(*args): raise SchemaError('字段定义无效')
        result=self.finished(self.submit(api=self.fake(fail)))
        self.assertEqual(result['error_status'],422)

    def test_cancel_preparation_does_not_write(self):
        entered = threading.Event()
        def prepare(*args):
            checkpoint('校验数据', 3, 10); entered.set(); self.release.wait(5)
            checkpoint('即将提交')
            self.scoped.insert('nodes', {'rows': [{'id': 1, 'name': 'bad'}]})
            return {'ok': True}
        task = self.submit(api=self.fake(prepare)); self.assertTrue(entered.wait(2))
        self.assertEqual(self.registry.get(task['id'], task['database'])['done'], 3)
        self.registry.cancel(task['id'], task['database']); self.release.set()
        self.assertEqual(self.finished(task)['status'], 'cancelled')
        with self.scoped.session('nodes') as table: self.assertEqual(table.all(), [])
        self.assertTrue(any(r['operation']=='取消后台任务' for r in self.scoped.audit_log.query()['logs']))

    def test_commit_rejects_cancel_and_finishes_once(self):
        entered = threading.Event()
        def write(*args):
            with committing('提交中'):
                entered.set(); self.release.wait(5)
                self.scoped.insert('nodes', {'rows': [{'id': 1, 'name': 'once'}]})
            return {'ok': True}
        task = self.submit(api=self.fake(write)); self.assertTrue(entered.wait(2))
        with self.assertRaises(APIError) as caught: self.registry.cancel(task['id'], task['database'])
        self.assertEqual(caught.exception.status, 409)
        self.release.set(); self.assertEqual(self.finished(task)['status'], 'done')
        with self.scoped.session('nodes') as table: self.assertEqual(len(table.all()), 1)

    def test_cancel_between_import_tables_keeps_completed_only(self):
        payload = {'format':'e', 'mode':'merge', 'text':'<first>\n@ id name\n# 1 A\n</first>\n<second>\n@ id name\n# 2 B\n</second>'}
        payload['token'] = DefinitionImporter(self.scoped).run(payload, preview=True)['token']
        entered = threading.Event()
        def pause(phase, done=0, total=0):
            if phase.startswith('准备提交表 second'): entered.set(); self.release.wait(5)
            checkpoint(phase, done, total)
        with patch('web.definition_import.checkpoint', side_effect=pause):
            task = self.submit('definition-import', payload)
            self.assertTrue(entered.wait(3))
            self.registry.cancel(task['id'], task['database']); self.release.set()
            result = self.finished(task)
        self.assertEqual(result['status'], 'cancelled', result)
        self.assertEqual(result['completed_tables'], ['first'])
        names = {t['name'] for t in self.scoped.tables()['tables']}
        self.assertIn('first', names); self.assertNotIn('second', names)
        with self.scoped.session('first') as table: self.assertEqual(table.all(), [{'id':1, 'name':'A'}])

    def test_cancel_logs_leaves_jsonl_and_index_rebuilds(self):
        for i in range(600):
            self.scoped.record_operation(category='data', operation='fixture', status='success', http_status=200, method='GET', path='/test')
        source = self.scoped.audit_log.path.read_bytes()
        entered = threading.Event()
        def pause(phase, done=0, total=0):
            if phase.startswith('建立日志索引'): entered.set(); self.release.wait(5)
            checkpoint(phase, done, total)
        with patch('web.audit_index.task_checkpoint', side_effect=pause):
            task=self.registry.submit(self.api, 'GET', ['logs'], {}, None)
            self.assertTrue(entered.wait(3)); self.registry.cancel(task['id'],task['database']); self.release.set()
            self.assertEqual(self.finished(task)['status'], 'cancelled')
        self.assertTrue(self.scoped.audit_log.path.read_bytes().startswith(source))
        self.assertEqual(self.scoped.audit_log.query(keyword='fixture')['total'], 600)

    def test_active_limit_and_queued_cancel(self):
        entered = threading.Event()
        def work(*args): entered.set(); self.release.wait(5); checkpoint('准备完成'); return {'ok':True}
        tasks = [self.submit(api=self.fake(work)) for _ in range(4)]
        self.assertTrue(entered.wait(2))
        with self.assertRaises(APIError) as caught: self.submit(api=self.fake(work))
        self.assertEqual(caught.exception.status, 429)
        self.registry.cancel(tasks[-1]['id'],tasks[-1]['database']); self.release.set()
        self.assertEqual(self.finished(tasks[-1])['status'],'cancelled')

    def test_completed_cache_is_bounded(self):
        for _ in range(12): self.finished(self.submit(api=self.fake(lambda *args:{'ok':True})))
        self.assertEqual(len(self.registry.tasks),8)


class TaskHTTPTests(unittest.TestCase):
    setUp = test_sqlite_server.SQLiteWebTests.setUp
    tearDown = test_sqlite_server.SQLiteWebTests.tearDown
    call = test_sqlite_server.SQLiteWebTests.call

    def test_bind_failure_preserves_original_error_and_cleans_resources(self):
        from web.sqlite_server import create_sqlite_server, SQLiteHTTPServer
        with patch.object(SQLiteHTTPServer,'server_bind',side_effect=OSError('port bind failure')):
            with self.assertRaisesRegex(OSError,'port bind failure'):
                create_sqlite_server('127.0.0.1',0,root=self.root/'occupied')

    def test_fast_path_and_server_timing(self):
        # Exercise the completed branch deterministically; scheduling on a busy
        # host may legitimately exceed the production 30 ms fast-path window.
        original=TaskRegistry.wait_initial
        with patch.object(TaskRegistry,'wait_initial',lambda registry,ident,database: original(registry,ident,database,5)):
            result=self.call('GET','/api/manage/logs?background=1')
        self.assertIn('logs',result)
        with urlopen(self.base+'/api/manage/info') as response:
            self.assertIn('app;dur=',response.headers['Server-Timing'])

    def test_async_polling_and_wrong_database(self):
        entered, release=threading.Event(),threading.Event()
        original=SQLiteManagement.dispatch
        def slow(api,method,parts,query,payload):
            if parts==['logs']: entered.set();release.wait(5)
            return original(api,method,parts,query,payload)
        try:
            with patch.object(SQLiteManagement,'dispatch',slow):
                result=self.call('GET','/api/manage/logs?background=1',status=202)
                task=result['task'];self.assertTrue(entered.is_set())
                self.call('GET','/api/manage/tasks/'+task['id']+'?database=wrong.sqlite3',status=404)
                current=self.call('GET','/api/manage/tasks/'+task['id'])
                self.assertEqual(current['status'],'running')
                release.set();self.server.tasks.wait_initial(task['id'],None,5)
                self.assertIn('logs',self.call('GET','/api/manage/tasks/'+task['id'])['result'])
        finally:release.set()
