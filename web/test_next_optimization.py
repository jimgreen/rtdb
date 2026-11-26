"""Scheduling, bounded delivery and lossless receipt lifecycle regressions."""
from contextlib import closing
import json
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web import test_database_scope, write_receipts
from web.management import SQLiteManagement
from web.tasks import TaskRegistry
from web.errors import APIError


class NextOptimizationTests(unittest.TestCase):
    setUp = test_database_scope.DatabaseScopeTests.setUp
    tearDown = test_database_scope.DatabaseScopeTests.tearDown

    def test_busy_database_does_not_occupy_both_workers(self):
        release, entered, second = threading.Event(), threading.Event(), threading.Event()
        registry = TaskRegistry()
        def block(*_): entered.set(); release.wait(5); return {'ok':True}
        a = self.manager.for_database('plant_a.db')
        b = self.manager.for_database('plant_b.db')
        def submit(manager, callback):
            return registry.submit(SimpleNamespace(manager=manager,dispatch=callback),'POST',['definition-preview'],{}, {})
        try:
            first=submit(a,block); self.assertTrue(entered.wait(2))
            queued=submit(a,lambda *_:second.set() or {'ok':True})
            other=submit(b,lambda *_:{'ok':True})
            self.assertEqual(registry.wait_initial(other['id'],b.database_file,2)['status'],'done')
            self.assertFalse(second.is_set())
            cancelled=registry.cancel(queued['id'],a.database_file)
            self.assertEqual(cancelled['status'],'cancelled')
            release.set()
            self.assertEqual(registry.wait_initial(first['id'],a.database_file,2)['status'],'done')
            self.assertFalse(second.is_set())
        finally: release.set(); registry.close()

    def test_long_poll_wakes_when_task_finishes(self):
        registry=TaskRegistry(); release=threading.Event()
        try:
            task=registry.submit(SimpleNamespace(manager=self.manager.for_database('plant_a.db'),
                dispatch=lambda *_:release.wait(5) and {'ok':True}),'POST',['definition-preview'],{}, {})
            ready=threading.Event(); answer=[]
            def wait():
                current=registry.get(task['id'],task['database']); ready.set()
                while current['status'] in ('queued','running'):
                    current=registry.wait_change(task['id'],task['database'],current['revision'],2)
                answer.append(current)
            thread=threading.Thread(target=wait);thread.start();self.assertTrue(ready.wait(2))
            release.set();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(answer[0]['status'],'done')
            self.assertIn('queue_ms',answer[0]['timings'])
        finally:release.set();registry.close()

    def seed(self, count):
        api=SQLiteManagement(self.manager.for_database('plant_a.db'))
        with patch.object(self.manager.audit_log,'write_event',return_value=None):
            for i in range(count): api.dispatch('POST',['tables','nodes','rows'],{}, {'rows':[{'id':i,'name':'fixture'}]})
        return api.manager

    def test_delivery_respects_batch_limit(self):
        manager=self.seed(5)
        self.assertTrue(write_receipts.publish(manager,recovery=True,batch_size=2))
        with closing(sqlite3.connect(manager.data_dir/manager.database_file)) as c:
            self.assertEqual(c.execute('SELECT SUM(delivered) FROM rtdb_web_commits').fetchone()[0],2)
        write_receipts.recover_pending(self.manager)
        self.assertEqual(sum(row['operation']=='添加数据' for row in manager.audit_log.read()),5)

    def test_archive_is_lossless_atomic_and_preserves_retry_guard(self):
        from web.receipt_maintenance import archive_batch, statistics
        manager=self.seed(3);write_receipts.recover_pending(self.manager)
        with closing(sqlite3.connect(manager.data_dir/manager.database_file)) as c,c:
            rows=c.execute('SELECT id,fingerprint,result,event FROM rtdb_web_commits').fetchall()
            for ident,_,_,encoded in rows:
                event=json.loads(encoded);event['timestamp']='2020-01-01T00:00:00+00:00'
                c.execute('UPDATE rtdb_web_commits SET event=? WHERE id=?',(json.dumps(event),ident))
            c.execute('UPDATE rtdb_web_commits SET delivered=0 WHERE id=?',(rows[-1][0],))
        first=archive_batch(manager,batch_size=1)
        self.assertEqual(first['archived'],1)
        self.assertEqual(statistics(manager)['archived'],1)
        archive_batch(manager,batch_size=10)
        self.assertEqual(statistics(manager)['archived'],2)
        self.assertEqual(statistics(manager)['pending'],1)
        ident,signature,result,_=rows[0]
        self.assertEqual(write_receipts.receipt(manager,ident,signature),json.loads(result))
        with self.assertRaises(APIError):write_receipts.receipt(manager,ident,'wrong')
        with closing(sqlite3.connect(manager.data_dir/manager.database_file)) as c:
            context={'enabled':True,'manager':manager,'id':ident,'fingerprint':signature}
            token=write_receipts._current.set(context)
            try:
                self.assertEqual(write_receipts.in_transaction(c,lambda _:self.fail('duplicate write')),json.loads(result))
            finally:write_receipts._current.reset(token)
        self.assertEqual(SQLiteManagement(manager).rows('nodes',{})['total'],3)

    def test_cell_edit_validation_runs_once_without_preview(self):
        api=SQLiteManagement(self.manager.for_database('plant_a.db'))
        with patch.object(api,'_prepare_cell_edits',wraps=api._prepare_cell_edits) as prepare:
            result=api.dispatch('PUT',['tables','nodes','cell-edits'],{}, {'inserts':[{'id':1,'name':'one'}]})
            self.assertEqual(result['inserted'],1);self.assertEqual(prepare.call_count,1)
        with self.assertRaises(APIError):
            api.dispatch('PUT',['tables','nodes','cell-edits'],{}, {'inserts':[{'id':2,'name':'two'},{'id':1,'name':'duplicate'}]})
        self.assertEqual(api.rows('nodes',{})['total'],1)

    def test_archive_failure_rolls_back_and_disabled_policy_preserves_details(self):
        from web.receipt_maintenance import archive_batch,configure,statistics
        manager=self.seed(2);write_receipts.recover_pending(self.manager)
        with closing(sqlite3.connect(manager.data_dir/manager.database_file)) as c,c:
            c.execute("UPDATE rtdb_web_commits SET event=json_set(event,'$.timestamp','2020-01-01T00:00:00Z')")
        import zlib
        compress=zlib.compress;calls=[]
        def fail_second(raw):
            calls.append(raw)
            if len(calls)==2:raise OSError('compression interrupted')
            return compress(raw)
        with patch('web.receipt_maintenance.zlib.compress',side_effect=fail_second):
            with self.assertRaises(OSError):archive_batch(manager)
        self.assertEqual(statistics(manager)['archived'],0)
        configure(manager,{'days':0});self.assertEqual(archive_batch(manager)['archived'],0)
        with self.assertRaises(APIError):configure(manager,{'days':True})
        with self.assertRaises(APIError):configure(manager,{'days':-1})
        configure(manager,{'days':7});self.assertEqual(archive_batch(manager)['archived'],2)

    def test_failed_delivery_backoff_does_not_block_other_database(self):
        a=self.seed(1);b=self.manager.for_database('plant_b.db')
        with patch.object(self.manager.audit_log,'write_event',return_value=None):
            SQLiteManagement(b).dispatch('POST',['tables','nodes','rows'],{}, {'rows':[{'id':1,'name':'other'}]})
        original=write_receipts.publish;delivered=threading.Event()
        def publish(manager,*args,**kwargs):
            if manager.database_file==a.database_file:return False
            result=original(manager,*args,**kwargs);delivered.set();return result
        delivery=None
        try:
            with patch('web.write_receipts.publish',side_effect=publish):
                delivery=write_receipts.AuditDelivery(self.manager)
                self.assertTrue(delivered.wait(1.5));self.assertFalse(write_receipts.pending(b))
                self.assertIn(a.database_file,delivery.status()['pending_databases'])
                self.assertIn(a.database_file,delivery.status()['error'])
        finally:
            if delivery:delivery.enqueue(a.database_file,True);delivery.close()

    def test_registry_startup_does_not_synchronously_replay_all_logs(self):
        self.seed(2)
        with patch('web.write_receipts.publish',side_effect=AssertionError('startup blocked on log replay')):
            registry=TaskRegistry(self.root/'tasks',self.manager);registry.close()

    def test_stale_maintenance_does_not_create_a_missing_database(self):
        from web.receipt_maintenance import statistics
        manager=SimpleNamespace(database_file='missing.db',data_dir=self.root/'data',read_scope=self.manager.read_scope)
        self.assertFalse(write_receipts.publish(manager))
        with self.assertRaises(APIError):statistics(manager)
        self.assertFalse((manager.data_dir/manager.database_file).exists())


class NextHTTPTests(unittest.TestCase):
    from web.test_sqlite_server import SQLiteWebTests
    setUp=SQLiteWebTests.setUp
    tearDown=SQLiteWebTests.tearDown
    call=SQLiteWebTests.call

    def test_maintenance_routes_are_database_scoped(self):
        path='/api/manage/receipt-maintenance?database=devices.db'
        self.assertEqual(self.call('GET',path)['days'],7)
        self.assertEqual(self.call('PUT',path,{'days':30}),{'days':30})
        self.assertEqual(self.call('GET',path)['days'],30)
        self.call('PUT',path,{'days':-1},status=422)
        self.call('GET','/api/manage/receipt-maintenance',status=400)
        self.assertTrue(self.call('POST',path,{})['queued'])

    def test_long_poll_returns_final_result_and_rejects_bad_wait(self):
        api=self.server.management;release=threading.Event();entered=threading.Event()
        def work(*_):entered.set();release.wait(5);return {'ok':True}
        task=self.server.tasks.submit(SimpleNamespace(manager=api.manager,dispatch=work),'POST',['definition-preview'],{}, {})
        try:
            self.assertTrue(entered.wait(2));current=self.server.tasks.get(task['id'],None)
            self.call('GET','/api/manage/tasks/'+task['id']+'?wait=NaN',status=400)
            timer=threading.Timer(.1,release.set);timer.start()
            result=self.call('GET','/api/manage/tasks/'+task['id']+'?wait=2&revision='+str(current['revision']))
            while result['status'] in ('queued','running'):
                result=self.call('GET','/api/manage/tasks/'+task['id']+'?wait=2&revision='+str(result['revision']))
            self.assertEqual(result['status'],'done');timer.join()
        finally:release.set()

    def test_expired_task_result_never_reexecutes_or_returns_internal_error(self):
        ident='9'*32;path='/api/manage/tables/devices/cell-edits?database=devices.db&background=1&request_id='+ident
        from web.test_sqlite_server import record
        payload={'inserts':[record(1)]}
        original=TaskRegistry.wait_initial
        with patch.object(TaskRegistry,'wait_initial',lambda registry,ident,database:original(registry,ident,database,5)):
            self.call('PUT',path,payload)
        self.server.tasks.tasks.pop(ident)
        with closing(self.server.tasks.store.connect()) as c,c:c.execute('UPDATE tasks SET result=NULL WHERE id=?',(ident,))
        response=self.call('PUT',path,payload,status=202)
        self.assertTrue(response['task']['result_expired']);self.assertEqual(response['task']['status'],'done')
        self.assertEqual(self.call('GET','/api/manage/tables/devices/rows?database=devices.db')['total'],1)


if __name__=='__main__':unittest.main()
