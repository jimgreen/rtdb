import copy
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from core.sqlite_db import SQLiteTable
from web import test_database_scope
from web.management import SQLiteManagement
from web.sqlite_service import fingerprint, browser_value, atomic_text
from web.errors import APIError
from web.definition_import import DefinitionImporter
import sqlite3
import multiprocessing
from web.sqlite_runtime import file_fence
from core import SQLiteDatabase
from web.sqlite_service import SQLiteManager


def hold_file_fence(path, started, release):
    with file_fence(path,False):
        started.set(); release.wait(10)


def external_write(data, definitions):
    with SQLiteDatabase(data,schema_directory=definitions,database_file='plant_a.db') as db:
        db['nodes'].kv.update(1,{'name':'external process'})


class WorkflowOptimizationTests(unittest.TestCase):
    setUp = test_database_scope.DatabaseScopeTests.setUp
    tearDown = test_database_scope.DatabaseScopeTests.tearDown

    def test_database_sessions_overlap(self):
        barrier=threading.Barrier(2)
        def read(filename):
            with self.manager.for_database(filename).session('nodes'):
                barrier.wait(timeout=2)
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(read,['plant_a.db','plant_b.db']))

    def test_schema_change_in_a_does_not_block_b(self):
        a=self.manager.for_database('plant_a.db')
        with ThreadPoolExecutor(1) as pool, a.exclusive():
            future=pool.submit(lambda:self.manager.for_database('plant_b.db').tables())
            self.assertEqual(len(future.result(timeout=2)['tables']),2)

    def test_same_database_readers_overlap(self):
        barrier=threading.Barrier(2)
        def read(_):
            with self.manager.for_database('plant_a.db').session('nodes'):
                barrier.wait(timeout=2)
        with ThreadPoolExecutor(2) as pool: list(pool.map(read,range(2)))

    def test_file_fence_shared_readers_exclude_schema_writers_across_processes(self):
        from core.sqlite_db import SQLiteBusyError
        context=multiprocessing.get_context('spawn')
        started,release=context.Event(),context.Event(); path=self.root/'scope.lock'
        process=context.Process(target=hold_file_fence,args=(path,started,release)); process.start()
        try:
            self.assertTrue(started.wait(5))
            with file_fence(path,False,timeout=.2): pass
            with self.assertRaises(SQLiteBusyError):
                with file_fence(path,True,timeout=.05): pass
        finally:
            release.set(); process.join(5)
            if process.is_alive(): process.terminate(); process.join()
        self.assertEqual(process.exitcode,0)

    def test_unchanged_query_skips_row_decode_and_external_write_invalidates(self):
        scoped=self.manager.for_database('plant_a.db'); api=SQLiteManagement(scoped)
        scoped.insert('nodes',{'rows':[{'id':1,'name':'A'}]})
        payload={'sql':'SELECT * FROM nodes','compact':True}
        initial=api.sql('nodes',payload)
        with patch.object(SQLiteTable,'_decode',side_effect=AssertionError('unchanged query must skip rows')):
            result=api.sql('nodes',{**payload,'if_version':initial['result_version']})
        self.assertTrue(result['unchanged'])
        with scoped._database() as database:
            database['nodes'].kv.update(1,{'name':'external'})
        changed=api.sql('nodes',{**payload,'if_version':initial['result_version']})
        self.assertEqual(changed['records'][0]['row']['name'],'external')

    def test_general_sql_never_uses_conditional_query_cache(self):
        api=SQLiteManagement(self.manager.for_database('plant_a.db'))
        payload={'sql':'SELECT random() AS value','compact':True}
        first=api.sql('nodes',payload)
        second=api.sql('nodes',{**payload,'if_version':first['result_version']})
        self.assertFalse(second.get('cache_hit'))
        self.assertNotEqual(first['rows'],second['rows'])

    def test_schema_change_closes_cache_and_invalidates_result(self):
        scoped=self.manager.for_database('plant_a.db'); api=SQLiteManagement(scoped)
        payload={'sql':'SELECT * FROM nodes','compact':True}
        first=api.sql('nodes',payload)
        old=scoped.definition('nodes').to_mapping(); desired=copy.deepcopy(old)
        desired['head']['chn_name']='新标题'
        scoped.save_schema('nodes',{'definition':desired,'version':fingerprint(old)})
        second=api.sql('nodes',{**payload,'if_version':first['result_version']})
        self.assertNotEqual(first['result_version'],second['result_version'])

    def test_validation_returns_draft_index_and_field(self):
        api=SQLiteManagement(self.manager.for_database('plant_a.db'))
        with self.assertRaises(APIError) as caught:
            api.validate_edits('nodes',{'inserts':[{'id':None,'name':'missing key'}]})
        self.assertEqual(caught.exception.details['issues'][0]['draft_index'],0)
        self.assertEqual(caught.exception.details['issues'][0]['fields'],['id'])

    def test_large_integer_keys_do_not_resend_unchanged_rows(self):
        scoped=self.manager.for_database('plant_a.db'); api=SQLiteManagement(scoped)
        scoped.insert('nodes',{'rows':[{'id':2**60,'name':'A'},{'id':2**60+1,'name':'B'}]})
        payload={'sql':'SELECT * FROM nodes','compact':True}
        first=browser_value(api.sql('nodes',payload))
        with scoped._database() as db: db['nodes'].kv.update(2**60,{'name':'changed'})
        delta=api.sql('nodes',{**payload,'if_version':first['result_version'], 'known_records':[r['ref'] for r in first['records']]})
        self.assertEqual(len(delta['records']),1)

    def test_closed_observer_stamp_cannot_cache_old_results(self):
        cache=self.manager._runtime.versions; filename='plant_a.db'
        _,before=cache.probe(filename,'test',None)
        cache.close(filename)
        cache.store(filename,'test',before,{'result_version':'old'})
        result,_=cache.probe(filename,'test','old')
        self.assertIsNone(result)

    def test_external_process_invalidates_query_cache(self):
        scoped=self.manager.for_database('plant_a.db'); api=SQLiteManagement(scoped)
        scoped.insert('nodes',{'rows':[{'id':1,'name':'A'}]})
        payload={'sql':'SELECT * FROM nodes','compact':True}
        first=api.sql('nodes',payload)
        process=multiprocessing.get_context('spawn').Process(target=external_write,args=(self.root/'data',self.root/'defs'))
        process.start(); process.join(10)
        if process.is_alive(): process.terminate(); process.join()
        self.assertEqual(process.exitcode,0)
        changed=api.sql('nodes',{**payload,'if_version':first['result_version']})
        self.assertEqual(changed['records'][0]['row']['name'],'external process')

    def test_conflict_contains_current_row_and_fields(self):
        scoped=self.manager.for_database('plant_a.db'); api=SQLiteManagement(scoped)
        scoped.insert('nodes',{'rows':[{'id':1,'name':'A'}]})
        old=api.rows('nodes',{})['records'][0]
        with scoped._database() as database:
            database['nodes'].kv.update(1,{'name':'other user'})
        with self.assertRaises(APIError) as caught:
            api.cell_edits('nodes',{'changes':[{'ref':old['ref'],'values':{'name':'mine'}}]})
        self.assertEqual(caught.exception.status,409)
        detail=caught.exception.details['conflicts'][0]
        self.assertEqual(detail['current']['name'],'other user')
        self.assertEqual(detail['fields'],['name'])

    def test_import_failure_rolls_back_schema_and_rows(self):
        scoped=self.manager.for_database('plant_a.db')
        scoped.insert('nodes',{'rows':[{'id':1,'name':'original'}]})
        importer=DefinitionImporter(scoped)
        payload={'format':'e','text':'<nodes>\n@ id name value\n# 1 changed 1.5\n</nodes>','mode':'merge'}
        preview=importer.run(payload,preview=True)
        with patch('web.atomic_import.write_embedded_definition',side_effect=OSError('injected after row writes')):
            result=importer.run({**payload,'token':preview['token']},preview=False)
        self.assertFalse(result['ok'])
        with scoped.session('nodes') as table:
            self.assertEqual(table.columns,('id','name'))
            self.assertEqual(table.all(),[{'id':1,'name':'original'}])
        self.assertEqual(len(scoped.tables()['tables']),2)

    def test_merge_updates_only_changed_rows_and_preserves_positions(self):
        scoped=self.manager.for_database('plant_a.db')
        scoped.insert('nodes',{'rows':[{'id':1,'name':'A'},{'id':2,'name':'B'}]})
        with scoped.session('nodes') as table:
            positions=table._connection().execute('SELECT _pos FROM rtdb_data_nodes ORDER BY _pos').fetchall()
        statements=[]; original=sqlite3.connect
        def traced(*args,**kwargs):
            connection=original(*args,**kwargs)
            if str(args[0]) == str(self.root/'data/plant_a.db'):
                connection.set_trace_callback(statements.append)
            return connection
        importer=DefinitionImporter(scoped)
        payload={'format':'e','text':'<nodes>\n@ id name\n# 2 changed\n</nodes>','mode':'merge'}
        preview=importer.run(payload,preview=True)
        with patch('web.atomic_import.sqlite3.connect',side_effect=traced):
            result=importer.run({**payload,'token':preview['token']},preview=False)
        self.assertTrue(result['ok'],result)
        self.assertFalse(any(s.startswith('DELETE FROM "rtdb_data_nodes"') for s in statements),statements)
        self.assertFalse(any(s.startswith('INSERT INTO "rtdb_data_nodes"') for s in statements),statements)
        with scoped.session('nodes') as table:
            self.assertEqual(table._connection().execute('SELECT _pos FROM rtdb_data_nodes ORDER BY _pos').fetchall(),positions)
            self.assertEqual(table.kv.get(1)['name'],'A')
            self.assertEqual(table.kv.get(2)['name'],'changed')

    def test_new_table_failure_leaves_no_table(self):
        scoped=self.manager.for_database('plant_a.db'); importer=DefinitionImporter(scoped)
        payload={'format':'e','text':'<fresh>\n@ id name\n# 1 A\n</fresh>','mode':'merge'}
        preview=importer.run(payload,preview=True)
        with patch('web.atomic_import.write_embedded_definition',side_effect=OSError('injected')):
            result=importer.run({**payload,'token':preview['token']},preview=False)
        self.assertFalse(result['ok'])
        self.assertEqual({item['name'] for item in scoped.tables()['tables']},{'nodes','lines'})

    def test_committed_import_export_failure_recovers_on_startup(self):
        scoped=self.manager.for_database('plant_a.db'); importer=DefinitionImporter(scoped)
        payload={'format':'e','text':'<nodes>\n@ id name value\n# 1 committed 1.5\n</nodes>','mode':'merge'}
        preview=importer.run(payload,preview=True)
        def fail_python_export(path, text):
            if path.suffix == '.py':
                raise OSError('export disk failure')
            atomic_text(path, text)
        with patch('web.sqlite_service.atomic_text',side_effect=fail_python_export):
            result=importer.run({**payload,'token':preview['token']},preview=False)
        self.assertFalse(result['ok']); self.assertIn('已提交',result['results'][0]['error'])
        self.assertTrue(scoped._journal.exists())
        recovered=SQLiteManager(self.root/'data',self.root/'defs')
        try:
            with recovered.for_database('plant_a.db').session('nodes') as table:
                self.assertEqual(table.all(),[{'id':1,'name':'committed','value':1.5}])
            self.assertFalse(scoped._journal.exists())
        finally: recovered.close()

    def test_import_into_legacy_file_preserves_sibling_sql_writes(self):
        from web.test_sqlite_multi_database import table_definition
        old=table_definition('legacy','旧表','id')
        self.manager.save_schema('legacy',{'definition':old},creating=True)
        scoped=self.manager.for_database('legacy.db')
        scoped.insert('legacy',{'rows':[{'id':1,'name':'A'}]})
        importer=DefinitionImporter(scoped)
        payload={'format':'e','text':'<fresh>\n@ id name\n# 1 new\n</fresh>','mode':'merge'}
        preview=importer.run(payload,preview=True)
        result=importer.run({**payload,'token':preview['token']},preview=False)
        self.assertTrue(result['ok'],result)
        api=SQLiteManagement(scoped)
        api.sql('legacy',{'sql':"INSERT INTO legacy(id,name) VALUES (2,'B')",'confirm':'legacy'})
        with scoped.session('legacy') as table:
            self.assertEqual(len(table),2)
            self.assertEqual(table.kv.get(1)['name'],'A')
