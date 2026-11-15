import copy
from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import runpy
import sqlite3
import threading
import unittest
from unittest.mock import patch

from core import SchemaError, TableDefinition
from core.sqlite_db import SQLiteTable
from web import test_database_scope, write_receipts
from web.errors import APIError
from web.management import SQLiteManagement
from web.sqlite_service import SQLiteManager, fingerprint
from web.tasks import TaskRegistry
from web.test_sqlite_multi_database import table_definition


def crash_schema(root, operation):
    root=Path(root);manager=SQLiteManager(root/'data',root/'defs');api=SQLiteManagement(manager.for_database('plant_a.sqlite3'))
    registry=TaskRegistry(root/'tasks',manager)
    if operation=='create':
        method='PUT';name='created';payload={'creating':True,'definition':table_definition(name,name,'id')}
    elif operation=='delete':
        method='DELETE';name='lines';payload={'confirm':name,'version':fingerprint(api.manager.definition(name).to_mapping())}
    else:
        method='PUT';name='nodes';desired=api.manager.definition(name).to_mapping();desired['head']['eng_name']='renamed'
        payload={'definition':desired,'version':fingerprint(api.manager.definition(name).to_mapping())}
    with patch('web.write_receipts.publish',side_effect=lambda *a,**kw:os._exit(9)):
        task=registry.submit(api,method,['tables',name,'schema'],{},payload,'d'*32)
        registry.wait_initial(task['id'],task['database'],10)


def crash_import(root):
    root=Path(root);manager=SQLiteManager(root/'data',root/'defs');api=SQLiteManagement(manager.for_database('plant_a.sqlite3'))
    payload={'format':'e','text':'<nodes>\n@ id name\n# 1 once\n</nodes>\n<lines>\n@ id name\n# 2 later\n</lines>','mode':'merge'}
    payload['token']=api.dispatch('POST',['definition-preview'],{},payload)['token']
    registry=TaskRegistry(root/'tasks',manager)
    with patch('web.definition_import.table_completed',side_effect=lambda *a:os._exit(9)):
        task=registry.submit(api,'POST',['definition-import'],{},payload,'e'*32)
        registry.wait_initial(task['id'],task['database'],10)


class SchemaReceiptIndexTests(unittest.TestCase):
    setUp=test_database_scope.DatabaseScopeTests.setUp
    tearDown=test_database_scope.DatabaseScopeTests.tearDown

    def api(self):return SQLiteManagement(self.manager.for_database('plant_a.sqlite3'))
    def save(self, desired, **extra):
        api=self.api()
        return api.dispatch('PUT',['tables','nodes','schema'],{},dict(definition=desired,version=fingerprint(api.manager.definition('nodes').to_mapping()),**extra))
    def index_columns(self):
        with self.api().manager.session('nodes') as table:
            connection=table._connection()
            return [tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{index[1]}")'))
                    for index in connection.execute(f'PRAGMA index_list({table._data_sql})').fetchall()]

    def test_optional_indexes_validate_and_roundtrip(self):
        original=self.api().manager.definition('nodes').to_mapping()
        self.assertNotIn('indexes',original)
        for invalid in ([['missing']],[[]],[['id','id']],[['name'],['name']],'name'):
            with self.subTest(invalid=invalid),self.assertRaises(SchemaError):
                TableDefinition.from_mapping({**original,'indexes':invalid},self.root)
        desired={**original,'indexes':[['name','id']]}
        self.save(desired)
        self.assertEqual(self.api().manager.definition('nodes').indexes,(('name','id'),))
        self.assertIn(('c1','c0'),self.index_columns())
        self.assertIn(('c0',),self.index_columns())
        exported=runpy.run_path(str(self.root/'defs/plant_a.py'))['DATABASE_DEFINITION']
        self.assertEqual(exported['tables'][0]['indexes'],[['name','id']])
        self.assertEqual(json.loads((self.root/'defs/plant_a.json').read_text('utf-8'))['tables'][0]['indexes'],[['name','id']])

    def test_index_only_change_never_scans_rows_and_preserves_data(self):
        self.api().manager.insert('nodes',{'rows':[{'id':1,'name':'preserved'}]})
        desired=self.api().manager.definition('nodes').to_mapping();desired['indexes']=[]
        with patch.object(SQLiteTable,'_select',side_effect=AssertionError('unexpected row scan')):self.save(desired)
        self.assertEqual(self.index_columns(),[('c0',)])
        self.assertEqual(self.api().rows('nodes',{})['records'][0]['row']['name'],'preserved')

    def test_rename_column_keeps_custom_index_and_stale_version_rejected(self):
        desired=self.api().manager.definition('nodes').to_mapping();old_version=fingerprint(desired)
        desired['columns'][1]['eng_name']='label';desired['indexes']=[['label']]
        self.save(desired,column_renames={'name':'label'})
        self.assertIn(('c1',),self.index_columns())
        with self.assertRaises(APIError):self.api().dispatch('PUT',['tables','nodes','schema'],{}, {'definition':desired,'version':old_version})

    def test_failed_schema_receipt_rolls_back_definition_and_indexes(self):
        before=self.api().manager.definition('nodes').to_mapping();desired={**before,'indexes':[]}
        with patch('web.write_receipts.result_summary',side_effect=RuntimeError('receipt failed')),self.assertRaises(RuntimeError):self.save(desired)
        self.assertEqual(self.api().manager.definition('nodes').to_mapping(),before)
        self.assertIn(('c1',),self.index_columns())

    def test_preview_reports_changes_and_populated_restrictions_without_writes(self):
        api=self.api();api.manager.insert('nodes',{'rows':[{'id':1,'name':'one'}]})
        before=api.manager.definition('nodes').to_mapping();desired=copy.deepcopy(before);desired['columns'][1]['data_type']='int'
        result=api.dispatch('POST',['tables','nodes','schema-preview'],{}, {'definition':desired,'version':fingerprint(before)})
        self.assertFalse(result['valid']);self.assertEqual(result['row_count'],1)
        self.assertEqual(api.manager.definition('nodes').to_mapping(),before)

    def test_create_rename_and_delete_recover_after_actual_process_exit(self):
        for operation in ('create','rename','delete'):
            with self.subTest(operation=operation):
                # Distinct ledger per process; the business receipt ID must also differ.
                root=self.root/operation;manager=SQLiteManager(root/'data',root/'defs')
                manager.create_database_file({'name':'plant_a','tables':[table_definition('nodes','节点','id'),table_definition('lines','线路','id')]});manager.close()
                child=multiprocessing.get_context('spawn').Process(target=crash_schema,args=(str(root),operation));child.start();child.join(15)
                if child.is_alive():child.terminate();child.join()
                self.assertEqual(child.exitcode,9)
                manager=SQLiteManager(root/'data',root/'defs');registry=TaskRegistry(root/'tasks',manager)
                try:
                    result=registry.get('d'*32,'plant_a.sqlite3');self.assertEqual(result['status'],'done')
                    names=[table['name'] for table in manager.for_database('plant_a.sqlite3').tables()['tables']]
                    if operation=='create':self.assertIn('created',names)
                    elif operation=='rename':self.assertIn('renamed',names);self.assertNotIn('nodes',names)
                    else:self.assertNotIn('lines',names)
                finally:registry.close();manager.close()

    def test_async_log_failure_does_not_block_data_save_and_later_drains(self):
        delivery=self.manager.audit_delivery=write_receipts.AuditDelivery(self.manager)
        entered=threading.Event();release=threading.Event();original=self.manager.audit_log.write_event
        def blocked(*args,**kwargs):entered.set();release.wait(5);return original(*args,**kwargs)
        try:
            with patch.object(self.manager.audit_log,'write_event',side_effect=blocked):
                result=self.api().dispatch('POST',['tables','nodes','rows'],{}, {'rows':[{'id':1,'name':'durable'}]})
                self.assertTrue(result['audit_pending']);self.assertTrue(entered.wait(2));self.assertFalse(delivery.drain(.02))
                with closing(sqlite3.connect(self.root/'data/plant_a.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT delivered FROM rtdb_web_commits').fetchone()[0],0)
                release.set();self.assertTrue(delivery.drain(3))
            self.assertFalse(delivery.status()['pending_databases'])
        finally:release.set();delivery.close()

    def test_pending_events_flush_as_one_batch(self):
        with patch.object(self.manager.audit_log,'write_event',return_value=None):
            for ident in range(3):self.api().dispatch('POST',['tables','nodes','rows'],{}, {'rows':[{'id':ident,'name':'batch'}]})
        original=self.manager.audit_log.write_events
        with patch.object(self.manager.audit_log,'write_events',wraps=original) as batch:
            self.assertTrue(write_receipts.publish(self.api().manager,recovery=True));self.assertEqual(batch.call_count,1)
        self.assertEqual(sum(row['operation']=='添加数据' for row in self.manager.audit_log.read()),3)

    def test_partial_import_recovers_committed_table_even_before_task_checkpoint(self):
        child=multiprocessing.get_context('spawn').Process(target=crash_import,args=(str(self.root),));child.start();child.join(15)
        if child.is_alive():child.terminate();child.join()
        self.assertEqual(child.exitcode,9)
        registry=TaskRegistry(self.root/'tasks',self.manager)
        try:
            task=registry.get('e'*32,'plant_a.sqlite3')
            self.assertEqual(task['status'],'interrupted');self.assertEqual(task['completed_tables'],['nodes'])
            self.assertEqual(task['details']['committed_results'],[{'name':'nodes','ok':True,'rows':1}])
            self.assertEqual(self.api().rows('nodes',{})['total'],1);self.assertEqual(self.api().rows('lines',{})['total'],0)
        finally:registry.close()

    def test_export_failure_reports_successful_commit_and_leaves_recoverable_exports(self):
        desired=self.api().manager.definition('nodes').to_mapping();desired['indexes']=[]
        from web.sqlite_service import atomic_text
        def fail_export(path,text):
            if path.suffix=='.py':raise OSError('disk unavailable')
            return atomic_text(path,text)
        with patch('web.sqlite_service.atomic_text',side_effect=fail_export):
            result=self.save(desired)
        self.assertIn('SQLite 已提交',result['export_warning'])
        self.api().rows('nodes',{})  # The next scoped access repairs the exported files.
        self.assertEqual(self.api().manager.definition('nodes').indexes,())
        self.assertEqual(runpy.run_path(str(self.root/'defs/plant_a.py'))['DATABASE_DEFINITION']['tables'][0]['indexes'],[])

    def test_query_plan_reflects_custom_index_and_rejects_writes(self):
        desired=self.api().manager.definition('nodes').to_mapping();desired['indexes']=[['name','id']];self.save(desired)
        result=self.api().dispatch('POST',['tables','nodes','query-plan'],{}, {'sql':"SELECT * FROM nodes WHERE name='A' AND id>0"})
        self.assertTrue(any('idx_c1_c0' in item['detail'] for item in result['plan']))
        with self.assertRaises(APIError):self.api().dispatch('POST',['tables','nodes','query-plan'],{}, {'sql':'DELETE FROM nodes'})
