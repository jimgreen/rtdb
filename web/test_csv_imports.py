"""File-backed CSV previews and atomic commits on isolated databases."""
import copy
import hashlib
import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from web import test_database_scope, database_uploads as uploads, csv_imports
from web.errors import APIError
from web.management import SQLiteManagement
from web.sqlite_service import fingerprint
from web.tasks import TaskCancelled


class CSVImportTests(unittest.TestCase):
    setUp=test_database_scope.DatabaseScopeTests.setUp
    tearDown=test_database_scope.DatabaseScopeTests.tearDown

    def upload(self,text,encoding='utf-8-sig'):
        data=text.encode(encoding);ident=uuid.uuid4().hex
        uploads.create(self.manager,dict(id=ident,kind='csv',filename='records.csv',size=len(data),database='plant_a.sqlite3',table='nodes',encoding=encoding))
        for offset in range(0,len(data),uploads.CHUNK):
            block=data[offset:offset+uploads.CHUNK];uploads.put_chunk(self.manager,ident,offset,block,hashlib.sha256(block).hexdigest())
        return ident

    def prepare(self,text,encoding='utf-8-sig'):
        ident=self.upload(text,encoding);return ident,uploads.finalize(self.manager,ident)

    def apply(self,ident,preview,mode='append',database='plant_a.sqlite3'):
        return SQLiteManagement(self.manager.for_database(database)).dispatch('POST',['tables','nodes','import'],{},
            dict(upload_id=ident,token=preview['token'],mode=mode,confirm='nodes'))

    def test_preview_then_append_isolated_and_replay_safe(self):
        ident,p=self.prepare('id,name\r\n1,"第一行\n第二行"\r\n2,\\\\N\r\n')
        self.assertEqual(p['rows'],2);self.assertEqual(p['sample'][0]['name'],'第一行\n第二行')
        self.assertEqual(self.manager.for_database('plant_a.sqlite3').rows('nodes')['rows'],[])
        result=self.apply(ident,p);self.assertEqual(result['imported'],2)
        self.assertTrue(self.apply(ident,p)['replayed'])
        self.assertEqual(len(self.manager.for_database('plant_a.sqlite3').rows('nodes')['rows']),2)
        self.assertEqual(self.manager.for_database('plant_b.sqlite3').rows('nodes')['rows'],[])
        self.assertTrue(any(row['status']=='success' and row['operation']=='加载数据文件' for row in self.manager.audit_log.read()))
        self.assertFalse(uploads.discard(self.manager,ident)['snapshot_retained'])
        self.assertEqual(len(self.manager.for_database('plant_a.sqlite3').rows('nodes')['rows']),2)

    def test_duplicate_across_batches_and_empty_keys_rejected_before_write(self):
        text='id,name\n'+''.join(f'{i},n\n' for i in range(1100))+'0,duplicate\n'
        for source in (text,'id,name\n\\N,missing\n','id,id\n1,2\n','id,name\n1,too,many\n','id,name\n1,"unterminated'):
            with self.subTest(source=source[-40:]):
                with self.assertRaises(APIError):uploads.finalize(self.manager,self.upload(source))
        self.assertEqual(self.manager.for_database('plant_a.sqlite3').rows('nodes')['rows'],[])

    def test_late_conflict_rolls_back_entire_append(self):
        scoped=self.manager.for_database('plant_a.sqlite3');scoped.insert('nodes',{'rows':[{'id':1100,'name':'old'}]})
        ident,p=self.prepare('id,name\n'+''.join(f'{i},new\n' for i in range(1101)))
        with self.assertRaises(Exception):self.apply(ident,p)
        self.assertEqual(scoped.rows('nodes')['rows'],[{'id':1100,'name':'old'}])

    def test_merge_keeps_positions_and_replace_checks_preview_revision(self):
        scoped=self.manager.for_database('plant_a.sqlite3');scoped.insert('nodes',{'rows':[{'id':1,'name':'old'}]})
        with scoped.session('nodes') as table:pos=table._connection().execute(f'SELECT _pos FROM {table._data_sql}').fetchone()[0]
        ident,p=self.prepare('id,name\n1,new\n2,second\n');self.apply(ident,p,'merge')
        with scoped.session('nodes') as table:self.assertEqual(table._connection().execute(f'SELECT _pos FROM {table._data_sql} WHERE c0=1').fetchone()[0],pos)
        ident,p=self.prepare('id,name\n3,replacement\n')
        scoped.insert('nodes',{'rows':[{'id':4,'name':'external'}]})
        with self.assertRaises(APIError) as error:self.apply(ident,p,'replace')
        self.assertEqual(error.exception.status,409)
        ident,p=self.prepare('id,name\n3,replacement\n');self.apply(ident,p,'replace')
        self.assertEqual(scoped.rows('nodes')['rows'],[{'id':3,'name':'replacement'}])

    def test_encoding_sample_bound_and_batch_memory(self):
        ident,p=self.prepare('id,name\n'+''.join(f'{i},中文名称\n' for i in range(1600)),'gb18030')
        self.assertEqual(len(p['sample']),20);self.assertEqual(p['rows'],1600)
        with patch.object(csv_imports,'BATCH',37):self.apply(ident,p)
        with self.manager.for_database('plant_a.sqlite3').session('nodes') as table:self.assertEqual(len(table),1600)

    def test_wrong_database_token_schema_or_prepared_file_rejected(self):
        ident,p=self.prepare('id,name\n1,new\n')
        for params in [(ident,{**p,'token':'wrong'},'plant_a.sqlite3'),(ident,p,'plant_b.sqlite3')]:
            with self.assertRaises(APIError):self.apply(params[0],params[1],database=params[2])
        path=uploads.session_path(self.manager,ident)/'prepared.sqlite3';path.write_bytes(b'broken')
        with self.assertRaises(APIError):self.apply(ident,p)
        ident,p=self.prepare('id,name\n1,new\n');scoped=self.manager.for_database('plant_a.sqlite3')
        old=scoped.definition('nodes').to_mapping();changed=copy.deepcopy(old);changed['head']['chn_name']='改变'
        scoped.save_schema('nodes',{'definition':changed,'version':fingerprint(old)})
        with self.assertRaises(APIError):self.apply(ident,p)

    def test_cancel_preview_retains_upload_and_retry_works(self):
        ident=self.upload('id,name\n1,new\n')
        with patch.object(csv_imports,'checkpoint',side_effect=TaskCancelled()):
            with self.assertRaises(TaskCancelled):uploads.finalize(self.manager,ident)
        self.assertEqual(uploads.status(self.manager,ident)['state'],'receiving')
        self.assertEqual(uploads.finalize(self.manager,ident)['rows'],1)

    def test_limits_identity_and_forbidden_finalize_kind(self):
        with self.assertRaises(APIError):uploads.create(self.manager,dict(id=uuid.uuid4().hex,kind='bad',filename='x.csv',size=1))
        with self.assertRaises(APIError):uploads.create(self.manager,dict(id=uuid.uuid4().hex,kind='csv',filename='x.csv',size=129*1024**2,database='plant_a.sqlite3',table='nodes'))
        ident=self.upload('id,name\n1,n\n2,n\n')
        with patch.object(csv_imports,'MAX_ROWS',1):
            with self.assertRaises(APIError):uploads.finalize(self.manager,ident)

    def test_preview_and_commit_background_results_survive_restart(self):
        from web.tasks import TaskRegistry
        import time
        ident=self.upload('id,name\n1,new\n');directory=self.root/'tasks'
        registry=TaskRegistry(directory,self.manager)
        def finish(task):
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                current=registry.get(task['id'],task['database'])
                if current['status'] not in ('queued','running'):break
                time.sleep(.02)
            self.assertEqual(current['status'],'done',current)
            return current
        try:
            preview_task=finish(registry.submit(SQLiteManagement(self.manager),'POST',['database-uploads',ident,'finalize'],{},{},uuid.uuid4().hex))
            payload=dict(upload_id=ident,token=preview_task['result']['token'],mode='append')
            applied=finish(registry.submit(SQLiteManagement(self.manager.for_database('plant_a.sqlite3')),'POST',['tables','nodes','import'],{},payload,uuid.uuid4().hex))
            for task in (preview_task,applied):
                signature=registry.tasks[task['id']]['fingerprint']
                interrupted={**task,'status':'interrupted'};interrupted.pop('result',None)
                registry.store.put(interrupted,signature)
            registry.close();registry=TaskRegistry(directory,self.manager)
            self.assertEqual(registry.get(preview_task['id'],None)['result']['rows'],1)
            self.assertEqual(registry.get(applied['id'],'plant_a.sqlite3')['result']['imported'],1)
        finally:registry.close()

    def test_types_and_composite_key_roundtrip(self):
        scoped=self.manager.for_database('plant_a.sqlite3');definition=scoped.definition('nodes').to_mapping()
        definition['columns'][1]['primary_key']=True
        for name,kind in [('value','float'),('enabled','bool'),('info','dict')]:
            definition['columns'].append(dict(eng_name=name,chn_name=name,data_type=kind,primary_key=False,disp_type='文本',disp_menu=''))
        scoped.save_schema('nodes',{'definition':definition,'version':fingerprint(scoped.definition('nodes').to_mapping())})
        ident,p=self.prepare('id,name,value,enabled,info\n9223372036854775807,中文,1.25,true,"{""a"":1}"\n9223372036854775807,另名,\\N,false,\\N\n')
        self.apply(ident,p)
        rows=scoped.rows('nodes')['rows'];self.assertEqual(rows[0]['info'],{'a':1});self.assertEqual(rows[0]['id'],2**63-1)
        self.assertIsNone(rows[1]['value']);self.assertFalse(rows[1]['enabled'])
        for value in ('nan','inf'):
            with self.assertRaises(APIError):uploads.finalize(self.manager,self.upload(f'id,name,value,enabled,info\n1,x,{value},true,{{}}\n'))

    def test_commit_failure_has_audit_record_and_no_partial_rows(self):
        ident,p=self.prepare('id,name\n'+''.join(f'{i},n\n' for i in range(1100)))
        def fail(phase,*args):
            if phase=='提交 CSV 记录':raise OSError('模拟写入中断')
        with patch.object(csv_imports,'checkpoint',side_effect=fail):
            with self.assertRaises(OSError):self.apply(ident,p)
        self.assertEqual(self.manager.for_database('plant_a.sqlite3').rows('nodes')['rows'],[])
        self.assertTrue(any(row['status']=='failure' and '模拟写入中断' in str(row['error']) for row in self.manager.audit_log.read()))
        self.assertEqual(self.apply(ident,p)['imported'],1100)


if __name__=='__main__':unittest.main()
