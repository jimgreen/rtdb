import json
import sqlite3
import threading
import multiprocessing
import os
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from web import test_database_scope, test_sqlite_server
from web.management import SQLiteManagement
from web.errors import APIError
from web.tasks import TaskRegistry, snapshot, persist
from web.task_store import TaskStore, request_fingerprint


def crash_after_commit(root, ident):
    from pathlib import Path
    from web.sqlite_service import SQLiteManager
    root=Path(root)
    manager=SQLiteManager(root/'data',root/'defs')
    scoped=manager.for_database('plant_a.sqlite3')
    registry=TaskRegistry(root/'tasks')
    def dispatch(*args):
        scoped.insert('nodes',{'rows':[{'id':1,'name':'committed before crash'}]})
        os._exit(7)
    task=registry.submit(SimpleNamespace(manager=scoped,dispatch=dispatch),'PUT',['tables','nodes','cell-edits'],{},{},ident)
    registry.wait_initial(task['id'],task['database'],10)


class PersistentTaskTests(unittest.TestCase):
    def setUp(self):
        test_database_scope.DatabaseScopeTests.setUp(self)
        self.directory=self.root/'tasks'
        self.registry=TaskRegistry(self.directory)
        self.scoped=self.manager.for_database('plant_a.sqlite3')
        self.calls=0
        def dispatch(*args):
            self.calls+=1
            self.scoped.insert('nodes',{'rows':[{'id':1,'name':'once'}]})
            return {'ok':True,'updated':1}
        self.api=SimpleNamespace(manager=self.scoped,dispatch=dispatch)
        self.ident='a'*32

    def tearDown(self):
        self.registry.close()
        test_database_scope.DatabaseScopeTests.tearDown(self)

    def submit(self,payload=None):
        return self.registry.submit(self.api,'PUT',['tables','nodes','cell-edits'],{},payload or {},self.ident)

    def finish(self,task):
        result=self.registry.wait_initial(task['id'],task['database'],5)
        self.assertEqual(result['status'],'done',result)
        return result

    def restart(self):
        self.registry.close();self.registry=TaskRegistry(self.directory)

    def test_simultaneous_duplicate_submissions_execute_once(self):
        with ThreadPoolExecutor(4) as pool: results=list(pool.map(lambda _:self.submit(),range(4)))
        self.finish(results[0])
        self.assertEqual({t['id'] for t in results},{self.ident});self.assertEqual(self.calls,1)

    def test_completed_result_survives_restart_and_blocks_replay(self):
        original=self.finish(self.submit())
        self.restart()
        result=self.submit()
        self.assertEqual(result['result'],original['result']);self.assertEqual(self.calls,1)
        with self.scoped.session('nodes') as table:self.assertEqual(len(table.all()),1)

    def test_different_payload_same_id_is_rejected(self):
        self.finish(self.submit())
        self.restart()
        with self.assertRaises(APIError) as caught:self.submit({'different':True})
        self.assertEqual(caught.exception.status,409);self.assertEqual(self.calls,1)

    def test_interrupted_receipt_is_not_reexecuted(self):
        task=dict(id=self.ident,database='plant_a.sqlite3',method='PUT',path='tables/nodes/cell-edits',
            created=1,status='running',phase='提交中',cancellable=False,completed_tables=['first'])
        signature=request_fingerprint('plant_a.sqlite3','PUT',['tables','nodes','cell-edits'],{},{})
        self.registry.store.put(task,signature)
        self.restart();result=self.submit()
        self.assertEqual(result['status'],'interrupted');self.assertEqual(result['completed_tables'],['first'])
        self.assertEqual(self.calls,0)

    def test_receipt_database_isolation_and_no_payload_storage(self):
        self.finish(self.submit({'secret_input':'not-stored'}))
        with self.assertRaises(APIError):self.registry.get(self.ident,'plant_b.sqlite3')
        with closing(sqlite3.connect(self.registry.store.path)) as c:
            stored=c.execute('SELECT state FROM tasks').fetchone()[0]
        self.assertNotIn('secret_input',stored)

    def test_expired_result_still_prevents_duplicate_execution(self):
        self.finish(self.submit())
        with closing(sqlite3.connect(self.registry.store.path)) as c,c:c.execute('UPDATE tasks SET updated=1')
        self.restart();result=self.submit()
        self.assertTrue(result['result_expired']);self.assertEqual(self.calls,1)

    def test_recent_list_omits_result_body(self):
        self.finish(self.submit())
        recent=self.registry.recent()[0]
        self.assertNotIn('result',recent);self.assertTrue(recent['result_available'])

    def test_persistence_failure_before_acceptance_performs_no_write(self):
        with patch.object(self.registry.store,'put',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.submit()
        self.assertEqual(self.calls,0)

    def test_second_executor_cannot_recover_live_tasks(self):
        from core.sqlite_db import SQLiteBusyError
        with self.assertRaises(SQLiteBusyError):TaskRegistry(self.directory)

    def test_process_crash_after_commit_is_not_replayed(self):
        self.registry.close()
        child=multiprocessing.get_context('spawn').Process(target=crash_after_commit,args=(str(self.root),self.ident))
        child.start();child.join(15)
        if child.is_alive():child.terminate();child.join()
        self.registry=TaskRegistry(self.directory)
        self.assertEqual(child.exitcode,7)
        self.assertEqual(self.submit()['status'],'interrupted');self.assertEqual(self.calls,0)
        with self.scoped.session('nodes') as table:self.assertEqual(table.all(),[{'id':1,'name':'committed before crash'}])


class PersistentTaskHTTPTests(unittest.TestCase):
    setUp=test_sqlite_server.SQLiteWebTests.setUp
    tearDown=test_sqlite_server.SQLiteWebTests.tearDown
    call=test_sqlite_server.SQLiteWebTests.call

    def test_put_receipt_and_duplicate_insert_protection(self):
        ident='b'*32
        path='/api/manage/tables/devices/cell-edits?database=devices.sqlite3&background=1&request_id='+ident
        payload={'inserts':[test_sqlite_server.record(77)]}
        original=TaskRegistry.wait_initial
        with patch.object(TaskRegistry,'wait_initial',lambda registry,ident,database:original(registry,ident,database,5)):
            first=self.call('PUT',path,payload)
            again=self.call('PUT',path,payload)
            self.assertEqual(first,again);self.assertEqual(first['inserted'],1)
            self.call('PUT',path,{'inserts':[test_sqlite_server.record(78)]},status=409)
        receipt=self.call('GET','/api/manage/tasks/'+ident+'?database=devices.sqlite3')
        self.assertEqual(receipt['status'],'done')
        self.assertEqual(self.call('GET','/api/manage/tables/devices/rows?database=devices.sqlite3')['total'],1)

    def test_post_background_and_scoped_cancel(self):
        entered,release=threading.Event(),threading.Event();original=SQLiteManagement.dispatch
        def slow(api,method,parts,query,payload):
            if parts==['backup']:entered.set();release.wait(5)
            return original(api,method,parts,query,payload)
        try:
            with patch.object(SQLiteManagement,'dispatch',slow):
                task=self.call('POST','/api/manage/backup?database=devices.sqlite3&background=1',{'tables':['devices']},status=202)['task']
                self.assertTrue(entered.is_set())
                self.call('POST','/api/manage/tasks/'+task['id']+'/cancel?database=devices.sqlite3',{})
                release.set();self.server.tasks.wait_initial(task['id'],'devices.sqlite3',5)
                self.assertEqual(self.call('GET','/api/manage/tasks/'+task['id']+'?database=devices.sqlite3')['status'],'cancelled')
        finally:release.set()
