import json
import multiprocessing
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from web import test_database_scope, test_sqlite_server, write_receipts
from web.management import SQLiteManagement
from web.tasks import TaskRegistry
from web.sqlite_service import SQLiteManager
from web.errors import APIError


def crash_with_receipt(root):
    root=Path(root); manager=SQLiteManager(root/'data',root/'defs')
    registry=TaskRegistry(root/'tasks',manager)
    with patch('web.write_receipts.publish',side_effect=lambda *a,**kw:os._exit(9)):
        task=registry.submit(SQLiteManagement(manager.for_database('plant_a.db')),'PUT',
            ['tables','nodes','cell-edits'],{}, {'inserts':[{'id':1,'name':'once'}]}, 'a'*32)
        registry.wait_initial(task['id'],task['database'],10)


class WriteReceiptTests(unittest.TestCase):
    setUp=test_database_scope.DatabaseScopeTests.setUp
    tearDown=test_database_scope.DatabaseScopeTests.tearDown

    def api(self): return SQLiteManagement(self.manager.for_database('plant_a.db'))

    def receipts(self):
        with closing(sqlite3.connect(self.root/'data/plant_a.db')) as c:
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(write_receipts.TABLE,)).fetchone()
            return c.execute(f'SELECT id,result,event,delivered FROM {write_receipts.TABLE}').fetchall() if exists else []

    def test_data_receipt_audit_are_committed_together(self):
        result=self.api().dispatch('PUT',['tables','nodes','cell-edits'],{}, {'inserts':[{'id':1,'name':'ok'}]})
        receipt=self.receipts()[0]; event=json.loads(receipt[2])
        self.assertEqual(json.loads(receipt[1]),result); self.assertEqual(receipt[3],1)
        self.assertEqual(event['database'],'plant_a.db'); self.assertEqual(event['request_id'],receipt[0])

    def test_invalid_batch_rolls_back_data_and_receipt(self):
        with self.assertRaises(APIError):
            self.api().dispatch('PUT',['tables','nodes','cell-edits'],{}, {'inserts':[{'id':1,'name':'a'},{'id':1,'name':'b'}]})
        self.assertFalse(self.receipts());self.assertEqual(self.api().manager.rows('nodes')['total'],0)

    def test_audit_event_failure_rolls_back_already_applied_data(self):
        with patch('web.write_receipts.result_summary',side_effect=OSError('outbox failure')):
            with self.assertRaises(OSError):
                self.api().dispatch('PUT',['tables','nodes','cell-edits'],{}, {'inserts':[{'id':1,'name':'never committed'}]})
        self.assertFalse(self.receipts());self.assertEqual(self.api().manager.rows('nodes')['total'],0)

    def test_log_failure_reports_committed_data_and_recovers_once(self):
        with patch.object(self.manager.audit_log,'write_event',return_value=None):
            result=self.api().dispatch('PUT',['tables','nodes','cell-edits'],{}, {'inserts':[{'id':1,'name':'ok'}]})
        self.assertIn('数据已提交',result['audit_warning']);self.assertEqual(self.receipts()[0][3],0)
        write_receipts.recover_pending(self.manager);write_receipts.recover_pending(self.manager)
        ident=self.receipts()[0][0]
        self.assertEqual(sum(r['id']==ident for r in self.manager.audit_log.read()),1)

    def test_crash_between_append_and_delivery_marker_does_not_duplicate_log(self):
        self.api().dispatch('POST',['tables','nodes','rows'],{}, {'rows':[{'id':1,'name':'ok'}]})
        ident=self.receipts()[0][0]
        with closing(sqlite3.connect(self.root/'data/plant_a.db')) as c,c:
            c.execute(f'UPDATE {write_receipts.TABLE} SET delivered=0')
        write_receipts.recover_pending(self.manager)
        self.assertEqual(sum(r['id']==ident for r in self.manager.audit_log.read()),1)

    def test_delete_and_sql_writes_have_receipts(self):
        self.api().dispatch('POST',['tables','nodes','rows'],{}, {'rows':[{'id':1,'name':'ok'}]})
        self.api().dispatch('POST',['tables','nodes','sql'],{}, {'sql':"UPDATE nodes SET name='new'",'confirm':'nodes'})
        row=self.api().rows('nodes',{})['records'][0]
        self.api().dispatch('DELETE',['tables','nodes','rows'],{}, {'records':[row['ref']]})
        self.assertEqual(len(self.receipts()),3);self.assertEqual(self.api().manager.rows('nodes')['total'],0)

    def test_actual_process_crash_recovers_success_without_reexecution(self):
        child=multiprocessing.get_context('spawn').Process(target=crash_with_receipt,args=(str(self.root),))
        child.start();child.join(15)
        if child.is_alive():child.terminate();child.join()
        self.assertEqual(child.exitcode,9)
        registry=TaskRegistry(self.root/'tasks',self.manager)
        try:
            result=registry.get('a'*32,'plant_a.db')
            self.assertEqual(result['status'],'done');self.assertEqual(result['result']['inserted'],1)
            again=registry.submit(self.api(),'PUT',['tables','nodes','cell-edits'],{}, {'inserts':[{'id':1,'name':'once'}]},'a'*32)
            self.assertEqual(again['status'],'done');self.assertEqual(self.api().manager.rows('nodes')['total'],1)
        finally:registry.close()


class ReceiptHTTPTests(unittest.TestCase):
    setUp=test_sqlite_server.SQLiteWebTests.setUp
    tearDown=test_sqlite_server.SQLiteWebTests.tearDown
    call=test_sqlite_server.SQLiteWebTests.call

    def test_delete_query_parameters_and_duplicate_delete_receipt(self):
        suffix='?database=devices.db'
        self.call('POST','/api/manage/tables/devices/rows'+suffix,{'rows':[test_sqlite_server.record(1)]})
        row=self.call('GET','/api/manage/tables/devices/rows'+suffix)['records'][0]
        endpoint='/api/manage/tables/devices/rows'+suffix+'&background=1&request_id='+'c'*32
        original=TaskRegistry.wait_initial
        with patch.object(TaskRegistry,'wait_initial',lambda registry,ident,database:original(registry,ident,database,5)):
            first=self.call('DELETE',endpoint,{'records':[row['ref']]})
            second=self.call('DELETE',endpoint,{'records':[row['ref']]})
        self.assertEqual(first,second);self.assertEqual(second['deleted'],1)
        task=self.server.tasks.get('c'*32,'devices.db')
        self.assertEqual(task['status'],'done');self.assertEqual(task['result']['deleted'],1)
