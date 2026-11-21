"""Disk-backed multi-table E import correctness and restart boundaries."""
import copy
import hashlib
import json
import io
import multiprocessing
import os
from pathlib import Path
import runpy
import unittest
import uuid
from unittest.mock import patch

from web import test_database_scope, database_uploads as uploads
from web.management import SQLiteManagement
from web.errors import APIError


def crash_after_first_efile_table(root,ident,token):
    from web import efile_imports
    from web.sqlite_service import SQLiteManager
    from web.tasks import TaskRegistry
    root=Path(root);manager=SQLiteManager(root/'data',root/'defs')
    api=SQLiteManagement(manager.for_database('plant_a.sqlite3'));original=efile_imports.apply_table
    def crash(manager,plan,*args):
        if plan['summary']['name']=='lines':os._exit(7)
        return original(manager,plan,*args)
    registry=TaskRegistry(root/'crash-tasks',manager)
    with patch.object(efile_imports,'apply_table',crash):
        task=registry.submit(api,'POST',['definition-import'],{},dict(upload_id=ident,token=token),'e'*32)
        registry.wait_initial(task['id'],task['database'],20)
    os._exit(8)


class EfileStreamTests(unittest.TestCase):
    setUp = test_database_scope.DatabaseScopeTests.setUp
    tearDown = test_database_scope.DatabaseScopeTests.tearDown

    def upload(self, text, encoding='utf-8-sig'):
        data=text.encode(encoding);ident=uuid.uuid4().hex
        uploads.create(self.manager,dict(id=ident,kind='e',filename='grid.e',size=len(data),database='plant_a.sqlite3',encoding='auto'))
        for offset in range(0,len(data),uploads.CHUNK):
            block=data[offset:offset+uploads.CHUNK]
            uploads.put_chunk(self.manager,ident,offset,block,hashlib.sha256(block).hexdigest())
        return ident,uploads.finalize(self.manager,ident)

    def request(self, action, payload, database='plant_a.sqlite3'):
        return SQLiteManagement(self.manager.for_database(database)).dispatch('POST',['definition-'+action],{},payload)

    def validate(self, ident, mode='merge', **choices):
        return self.request('preview',dict(upload_id=ident,mode=mode,**choices))

    def apply(self, ident, preview, **extra):
        return self.request('import',dict(upload_id=ident,token=preview['token'],**extra))

    def rows(self, name):
        with self.manager.for_database('plant_a.sqlite3').session(name) as table:return table.all()

    def test_multi_table_inference_preview_and_replay(self):
        ident,initial=self.upload("<meters>\n@ code id value\n# A 2 1\n# B 3 2.5\n</meters>\n<nodes>\n@ id name\n# 1 '北侧\n电表'\n</nodes>")
        cols=initial['tables'][0]['definition']['columns']
        self.assertTrue(cols[0]['primary_key']);self.assertFalse(cols[1]['primary_key'])
        self.assertEqual(cols[2]['data_type'],'float');self.assertEqual(self.rows('nodes'),[])
        preview=self.validate(ident);self.assertTrue(self.apply(ident,preview)['ok'])
        self.assertEqual(self.rows('nodes')[0]['name'],'北侧\n电表')
        replay=self.apply(ident,preview);self.assertTrue(all(r['replayed'] for r in replay['results']))
        self.assertEqual(len(self.rows('meters')),2)
        document=json.loads((self.root/'defs/plant_a.json').read_text(encoding='utf-8'))
        self.assertEqual(runpy.run_path(str(self.root/'defs/plant_a.py'))['DATABASE_DEFINITION'],document)

    def test_choices_validate_all_rows_and_reject_stale_preview(self):
        ident,initial=self.upload('<meters>\n@ code reading\n# 1 1\n# 2 2.5\n</meters>')
        columns=copy.deepcopy(initial['tables'][0]['definition']['columns']);columns[1]['data_type']='int'
        with self.assertRaises(Exception):self.validate(ident,definition_overrides={'meters':{'columns':columns}})
        self.assertNotIn('meters',[t['name'] for t in self.manager.for_database('plant_a.sqlite3').tables()['tables']])
        columns[1]['data_type']='str';columns[0]['primary_key']=False;columns[1]['primary_key']=True
        preview=self.validate(ident,definition_overrides={'meters':{'columns':columns}})
        self.assertTrue(self.apply(ident,preview)['ok']);self.assertEqual(self.rows('meters')[1]['reading'],'2.5')
        ident,_=self.upload('<nodes>\n@ id name\n# 1 new\n</nodes>');preview=self.validate(ident)
        self.manager.for_database('plant_a.sqlite3').insert('nodes',{'rows':[{'id':9,'name':'external'}]})
        self.assertFalse(self.apply(ident,preview)['ok']);self.assertEqual(self.rows('nodes')[0]['name'],'external')

    def test_partial_commit_retry_skips_completed_even_after_later_edit(self):
        from web import efile_imports
        ident,_=self.upload('<nodes>\n@ id name\n# 1 first\n</nodes>\n<lines>\n@ id name\n# 2 second\n</lines>')
        preview=self.validate(ident);original=efile_imports.apply_table
        def fail(manager,plan,*args):
            if plan['summary']['name']=='lines':raise RuntimeError('injected failure')
            return original(manager,plan,*args)
        with patch.object(efile_imports,'apply_table',fail):result=self.apply(ident,preview)
        self.assertFalse(result['ok']);self.assertEqual(self.rows('lines'),[])
        self.manager.for_database('plant_a.sqlite3').insert('nodes',{'rows':[{'id':3,'name':'later'}]})
        result=self.apply(ident,preview);self.assertTrue(result['ok']);self.assertTrue(result['results'][0]['replayed'])
        self.assertEqual(len(self.rows('nodes')),2);self.assertEqual(len(self.rows('lines')),1)
        with self.assertRaises(APIError):self.validate(ident,mode='replace')

    def test_duplicate_policies_positions_and_sibling_isolation(self):
        scoped=self.manager.for_database('plant_a.sqlite3')
        scoped.insert('nodes',{'rows':[{'id':1,'name':'old'},{'id':8,'name':'keep'}]})
        with scoped.session('nodes') as table:old_pos=dict(table._connection().execute(f'SELECT c0,_pos FROM {table._data_sql}'))
        source='<nodes>\n@ id name\n# 1 first\n# 2 second\n# 1 last\n</nodes>'
        for mode in ('append','merge','replace'):
            ident,_=self.upload(source);preview=self.validate(ident,mode)
            self.assertEqual(preview['tables'][0]['duplicates'],1)
            self.assertTrue(preview['tables'][0]['warnings']);self.assertTrue(self.apply(ident,preview)['ok'])
            rows={r['id']:r['name'] for r in self.rows('nodes')}
            self.assertEqual(rows[1],'old' if mode=='append' else 'last')
            self.assertEqual(8 in rows,mode!='replace')
            with scoped.session('nodes') as table:self.assertEqual(table._connection().execute(f'SELECT _pos FROM {table._data_sql} WHERE c0=1').fetchone()[0],old_pos[1])
        self.assertEqual(self.rows('lines'),[])
        self.assertEqual(self.manager.for_database('plant_b.sqlite3').rows('nodes')['rows'],[])

    def test_late_transaction_failure_rolls_back_schema_and_rows(self):
        ident,_=self.upload('<nodes>\n@ id name extra\n# 1 changed 2.5\n</nodes>');preview=self.validate(ident)
        with patch('web.write_receipts.record_step',side_effect=RuntimeError('injected after data')):result=self.apply(ident,preview)
        self.assertFalse(result['ok']);self.assertEqual(self.rows('nodes'),[])
        self.assertNotIn('extra',self.manager.for_database('plant_a.sqlite3').definition('nodes').column_names)
        self.assertTrue(self.apply(ident,preview)['ok']);self.assertEqual(self.rows('nodes')[0]['extra'],2.5)
        self.assertTrue(any(e['status']=='failure' for e in self.manager.audit_log.read()))

    def test_encoding_quotes_and_parser_parity(self):
        from core.stream_efile import events
        from core.definition_files import parse_modules
        text="<! version=1 !>\n<sample name='样本'>\n@ code value flag label\n// 编码 数值 状态 名称\n# 001 2 true 'O''Brien\n北侧'\n# 002 3.5 false ''\n</sample>"
        expected=parse_modules(text)[0]['rows']
        self.assertEqual([(line,row) for kind,line,row in events(io.StringIO(text)) if kind=='row'],expected)
        for encoding in ('utf-16','gb18030','utf-8-sig'):
            ident,initial=self.upload(text,encoding);cols=initial['tables'][0]['definition']['columns']
            self.assertEqual([c['data_type'] for c in cols],['str','float','bool','str'])
            preview=self.validate(ident);self.assertTrue(self.apply(ident,preview)['ok'])
            self.assertEqual(self.rows('sample')[0]['label'],"O'Brien\n北侧")

    def test_rejects_wrong_target_tokens_and_tampered_staging(self):
        ident,_=self.upload('<nodes>\n@ id name\n# 1 x\n</nodes>')
        with self.assertRaises(APIError):self.request('preview',{'upload_id':ident},'plant_b.sqlite3')
        preview=self.validate(ident)
        with self.assertRaises(APIError):self.apply(ident,{'token':'wrong'})
        path=uploads.session_path(self.manager,ident)/'prepared.sqlite3';path.write_bytes(b'bad')
        with self.assertRaises(APIError):self.apply(ident,preview)
        path=uploads.session_path(self.manager,ident)/'raw.sqlite3';path.write_bytes(b'bad')
        with self.assertRaises(APIError):self.validate(ident)

    def test_type_failure_clears_executable_preview_and_key_empty_rejected(self):
        ident,initial=self.upload('<meters>\n@ id value\n# 1 1.5\n</meters>');previous=self.validate(ident)
        columns=copy.deepcopy(initial['tables'][0]['definition']['columns']);columns[1]['data_type']='int'
        with self.assertRaises(APIError):self.validate(ident,definition_overrides={'meters':{'columns':columns}})
        with self.assertRaises(APIError):self.apply(ident,previous)
        ident,_=self.upload("<meters>\n@ id value\n# '' 1\n</meters>")
        with self.assertRaises(APIError):self.validate(ident)

    def test_limits_malformed_input_and_cancel_retry(self):
        from web import efile_imports
        from web.tasks import TaskCancelled
        for source in ('<x>\n@ id\n# 1 2\n</x>','<x>\n@ id\n# 1',"<x>\n@ id\n# 'broken",'<x>\n@ id id\n</x>'):
            with self.assertRaises(Exception):self.upload(source)
        with patch.object(efile_imports,'MAX_ROWS',1):
            with self.assertRaises(APIError):self.upload('<x>\n@ id\n# 1\n# 2\n</x>')
        # Parse cancellation retains bytes and returns the upload to a resumable state.
        original=efile_imports.prepare
        def cancelled(manager,value):
            with patch.object(efile_imports,'checkpoint',side_effect=TaskCancelled):return original(manager,value)
        ident=uuid.uuid4().hex;data=b'<x>\n@ id\n# 1\n</x>'
        uploads.create(self.manager,dict(id=ident,kind='e',filename='x.e',size=len(data),database='plant_a.sqlite3'))
        uploads.put_chunk(self.manager,ident,0,data,hashlib.sha256(data).hexdigest())
        with patch.object(efile_imports,'prepare',cancelled):
            with self.assertRaises(TaskCancelled):uploads.finalize(self.manager,ident)
        self.assertEqual(uploads.status(self.manager,ident)['state'],'receiving')
        self.assertEqual(uploads.finalize(self.manager,ident)['tables'][0]['incoming'],1)

    def test_empty_replace_preserves_sequence_and_export_recovery(self):
        scoped=self.manager.for_database('plant_a.sqlite3');scoped.insert('nodes',{'rows':[{'id':1,'name':'x'}]})
        ident,_=self.upload('<nodes>\n@ id name extra\n</nodes>');preview=self.validate(ident,'replace')
        # Structural rebuild is empty: the original AUTOINCREMENT high-water mark survives.
        self.assertTrue(self.apply(ident,preview)['ok']);scoped.insert('nodes',{'rows':[{'id':2,'name':'new','extra':None}]})
        with scoped.session('nodes') as table:self.assertGreater(table._connection().execute(f'SELECT _pos FROM {table._data_sql}').fetchone()[0],1)

    def test_populated_schema_protection_and_bounded_sample(self):
        scoped=self.manager.for_database('plant_a.sqlite3');scoped.insert('nodes',{'rows':[{'id':1,'name':'x'}]})
        ident,initial=self.upload('<nodes>\n@ id name\n'+''.join(f'# {i} n\n' for i in range(2,1502))+'</nodes>')
        self.assertEqual(len(initial['tables'][0]['sample']),5)
        columns=copy.deepcopy(initial['tables'][0]['definition']['columns']);columns[0]['data_type']='str'
        with self.assertRaises(APIError):self.validate(ident,definition_overrides={'nodes':{'columns':columns}})
        preview=self.validate(ident);self.assertTrue(self.apply(ident,preview)['ok']);self.assertEqual(len(self.rows('nodes')),1501)

    def test_partial_task_recovery_and_resubmission(self):
        from web import efile_imports
        from web.tasks import TaskRegistry
        ident,_=self.upload('<nodes>\n@ id name\n# 1 a\n</nodes>\n<lines>\n@ id name\n# 2 b\n</lines>')
        preview=self.validate(ident);directory=self.root/'tasks';registry=TaskRegistry(directory,self.manager)
        api=SQLiteManagement(self.manager.for_database('plant_a.sqlite3'));payload=dict(upload_id=ident,token=preview['token']);original=efile_imports.apply_table
        def fail(manager,plan,*args):
            if plan['summary']['name']=='lines':raise RuntimeError('interruption')
            return original(manager,plan,*args)
        try:
            with patch.object(efile_imports,'apply_table',fail):
                task=registry.submit(api,'POST',['definition-import'],{},payload,uuid.uuid4().hex)
                done=registry.wait_initial(task['id'],task['database'],15)
            self.assertEqual(done['status'],'done');self.assertFalse(done['result']['ok'])
            signature=registry.tasks[task['id']]['fingerprint'];interrupted={**done,'status':'interrupted'};interrupted.pop('result')
            registry.store.put(interrupted,signature);registry.close();registry=TaskRegistry(directory,self.manager)
            recovered=registry.get(task['id'],task['database']);self.assertEqual(recovered['details']['completed_tables'],['nodes'])
            retry=registry.submit(api,'POST',['definition-import'],{},payload,uuid.uuid4().hex)
            result=registry.wait_initial(retry['id'],retry['database'],15)
            self.assertTrue(result['result']['ok']);self.assertTrue(result['result']['results'][0]['replayed'])
        finally:registry.close()

    def test_committed_export_failure_is_reported_and_recoverable(self):
        from web.sqlite_service import SQLiteManager
        ident,_=self.upload('<nodes>\n@ id name\n# 1 a\n</nodes>');preview=self.validate(ident)
        original=SQLiteManager._recover
        def fail_export(manager):
            if manager._journal.exists():raise OSError('export unavailable')
            return original(manager)
        with patch.object(SQLiteManager,'_recover',fail_export):result=self.apply(ident,preview)
        self.assertTrue(result['ok']);self.assertIn('已提交',result['results'][0]['export_warning'])
        result=self.apply(ident,preview);self.assertTrue(result['results'][0]['replayed']);self.assertEqual(len(self.rows('nodes')),1)

    def test_real_process_crash_after_first_table_resumes_without_replay(self):
        from web.tasks import TaskRegistry
        ident,_=self.upload('<nodes>\n@ id name\n# 1 first\n</nodes>\n<lines>\n@ id name\n# 2 second\n</lines>')
        preview=self.validate(ident)
        process=multiprocessing.get_context('spawn').Process(target=crash_after_first_efile_table,args=(str(self.root),ident,preview['token']))
        process.start();process.join(25)
        if process.is_alive():process.terminate();process.join();self.fail('isolated crash helper timed out')
        self.assertEqual(process.exitcode,7)
        self.assertEqual(len(self.rows('nodes')),1);self.assertEqual(self.rows('lines'),[])
        registry=TaskRegistry(self.root/'crash-tasks',self.manager)
        try:
            recovered=registry.get('e'*32,'plant_a.sqlite3')
            self.assertEqual(recovered['details']['completed_tables'],['nodes'])
            result=self.apply(ident,preview);self.assertTrue(result['ok']);self.assertTrue(result['results'][0]['replayed'])
            self.assertEqual(len(self.rows('nodes')),1);self.assertEqual(len(self.rows('lines')),1)
        finally:registry.close()
