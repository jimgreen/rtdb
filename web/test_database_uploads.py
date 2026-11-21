"""Resumable, bounded SQLite snapshot uploads on isolated files."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from web import test_database_scope, database_uploads as uploads, database_snapshots
from web.errors import APIError
from web.sqlite_service import SQLiteManager


class UploadTests(unittest.TestCase):
    setUp = test_database_scope.DatabaseScopeTests.setUp
    tearDown = test_database_scope.DatabaseScopeTests.tearDown

    def source(self):
        scoped=self.manager.for_database('plant_a.sqlite3')
        scoped.insert('nodes',{'rows':[{'id':1,'name':'上传内容'}]})
        with closing(sqlite3.connect(scoped.data_dir/scoped.database_file)) as db, db:
            db.execute('CREATE TABLE extra_blob(value BLOB)')
            db.execute('INSERT INTO extra_blob VALUES(zeroblob(?))',(uploads.CHUNK*2,))
        item=database_snapshots.create(scoped)
        return database_snapshots.resolve(scoped,item['id'])[0].read_bytes()

    def init(self,data,ident='a'*32):
        return uploads.create(self.manager,dict(id=ident,filename='uploaded.sqlite3',size=len(data),last_modified=123))

    def send(self,ident,data,start=0):
        for offset in range(start,len(data),uploads.CHUNK):
            block=data[offset:offset+uploads.CHUNK]
            uploads.put_chunk(self.manager,ident,offset,block,hashlib.sha256(block).hexdigest())

    def test_resume_after_manager_restart_and_publish_restore(self):
        data=self.source();session=self.init(data);ident=session['id'];block=data[:uploads.CHUNK]
        uploads.put_chunk(self.manager,ident,0,block,hashlib.sha256(block).hexdigest())
        other=SQLiteManager(self.manager.data_dir,self.manager.schema_dir)
        try:
            status=uploads.status(other,ident)
            self.assertEqual(status['received'],len(block));self.assertEqual(len(status['chunks']),1)
            self.send(ident,data,len(block));result=uploads.finalize(other,ident)
            self.assertEqual(result['sha256'],hashlib.sha256(data).hexdigest())
            self.assertEqual(uploads.finalize(other,ident)['id'],result['id'])
            scoped=database_snapshots.scope(other,result['database'])
            database_snapshots.restore(scoped,result['id'],{'name':'uploaded_copy'})
            self.assertEqual(other.for_database('uploaded_copy.sqlite3').rows('nodes')['rows'][0]['name'],'上传内容')
        finally:other.close()

    def test_duplicate_chunks_are_idempotent_and_mismatch_rejected(self):
        data=self.source();ident=self.init(data)['id'];block=data[:uploads.CHUNK];digest=hashlib.sha256(block).hexdigest()
        with ThreadPoolExecutor(2) as pool:
            answers=list(pool.map(lambda _:uploads.put_chunk(self.manager,ident,0,block,digest),range(2)))
        self.assertEqual([r['received'] for r in answers],[uploads.CHUNK]*2)
        changed=b'X'+block[1:]
        with self.assertRaises(APIError):uploads.put_chunk(self.manager,ident,0,changed,hashlib.sha256(changed).hexdigest())
        with self.assertRaises(APIError):uploads.put_chunk(self.manager,ident,uploads.CHUNK,block,'0'*64)
        self.assertEqual(uploads.status(self.manager,ident)['received'],uploads.CHUNK)

    def test_order_size_and_unfinished_finalize(self):
        data=self.source();ident=self.init(data)['id'];block=data[:uploads.CHUNK]
        for offset,value in [(uploads.CHUNK,block),(1,block),(0,block[:10]),(0,block+b'x')]:
            with self.assertRaises(APIError):uploads.put_chunk(self.manager,ident,offset,value,hashlib.sha256(value).hexdigest())
        with self.assertRaises(APIError):uploads.finalize(self.manager,ident)
        self.assertEqual(uploads.status(self.manager,ident)['received'],0)

    def test_unacknowledged_tail_is_replaced_on_resume(self):
        data=self.source();ident=self.init(data)['id']
        path=uploads.session_path(self.manager,ident)/'payload.sqlite3'
        path.write_bytes(b'partial crashed write')
        self.send(ident,data)
        self.assertEqual(path.read_bytes(),data)

    def test_disk_corruption_detected_before_snapshot_publication(self):
        data=self.source();ident=self.init(data)['id'];self.send(ident,data)
        path=uploads.session_path(self.manager,ident)/'payload.sqlite3'
        with path.open('r+b') as f:f.seek(-1,2);f.write(b'X')
        with self.assertRaises(APIError):uploads.finalize(self.manager,ident)
        self.assertEqual(uploads.status(self.manager,ident)['state'],'error')
        self.assertFalse((self.manager.data_dir/'.web-snapshots'/f'{ident}.json').exists())

    def test_invalid_file_and_expected_hash_rejected(self):
        data=b'not a sqlite file';ident=self.init(data)['id'];self.send(ident,data)
        with self.assertRaises(APIError):uploads.finalize(self.manager,ident)
        data=self.source();ident='b'*32
        uploads.create(self.manager,dict(id=ident,filename='snapshot.sqlite3',size=len(data),sha256='0'*64))
        self.send(ident,data)
        with self.assertRaises(APIError):uploads.finalize(self.manager,ident)

    def test_identity_quota_and_path_checks(self):
        self.init(b'a')
        with self.assertRaises(APIError):self.init(b'aa')
        for value in ('../escape','../../x.sqlite3','x-wal','x.sqlite3/other'):
            with self.assertRaises(APIError):uploads.create(self.manager,dict(id='b'*32,filename=value,size=1))
        with self.assertRaises(APIError):uploads.status(self.manager,'../x')
        with patch.object(uploads,'MAX_SESSIONS',1):
            with self.assertRaises(APIError):self.init(b'b','b'*32)
        with self.assertRaises(APIError):uploads.create(self.manager,dict(id='b'*32,filename='x.sqlite3',size=uploads.MAX_BYTES+1))

    def test_expiry_and_discard_only_remove_staging(self):
        data=self.source();ident=self.init(data)['id'];self.send(ident,data);result=uploads.finalize(self.manager,ident)
        uploads.discard(self.manager,ident)
        self.assertTrue((self.manager.data_dir/'.web-snapshots'/f'{result["id"]}.sqlite3').is_file())
        self.init(b'a','c'*32)
        with patch.object(uploads.time,'time',return_value=9999999999):
            with self.assertRaises(APIError) as error:uploads.status(self.manager,'c'*32)
            self.assertEqual(error.exception.status,410)
            self.init(b'b','d'*32)
        self.assertFalse(uploads.session_path(self.manager,'c'*32).exists())

    def test_cancel_validation_keeps_uploaded_bytes_for_retry(self):
        from web.tasks import TaskCancelled
        data=self.source();ident=self.init(data)['id'];self.send(ident,data)
        with patch.object(uploads,'checkpoint',side_effect=TaskCancelled()):
            with self.assertRaises(TaskCancelled):uploads.finalize(self.manager,ident)
        self.assertEqual(uploads.status(self.manager,ident)['state'],'receiving')
        self.assertEqual(uploads.status(self.manager,ident)['received'],len(data))
        self.assertEqual(uploads.finalize(self.manager,ident)['sha256'],hashlib.sha256(data).hexdigest())

    def test_crash_between_hardlink_and_manifest_can_finish_publication(self):
        import os
        data=self.source();ident=self.init(data)['id'];self.send(ident,data)
        source=uploads.session_path(self.manager,ident)/'payload.sqlite3'
        target=database_snapshots.directory(self.manager)/(ident+'.sqlite3')
        os.link(source,target)
        result=uploads.finalize(self.manager,ident)
        self.assertEqual(result['sha256'],hashlib.sha256(data).hexdigest())
        self.assertTrue(target.with_suffix('.json').is_file())
        self.assertFalse(source.exists())

    def test_publication_does_not_replace_unrelated_snapshot(self):
        data=self.source();ident=self.init(data)['id'];self.send(ident,data)
        target=database_snapshots.directory(self.manager)/(ident+'.sqlite3')
        target.write_bytes(b'unrelated')
        with self.assertRaises(APIError) as error:uploads.finalize(self.manager,ident)
        self.assertEqual(error.exception.status,409)
        self.assertEqual(target.read_bytes(),b'unrelated')
        self.assertFalse(target.with_suffix('.json').exists())

    def test_acknowledgement_write_failure_does_not_advance_offset(self):
        data=self.source();ident=self.init(data)['id'];block=data[:uploads.CHUNK];digest=hashlib.sha256(block).hexdigest()
        with patch.object(uploads,'_save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):uploads.put_chunk(self.manager,ident,0,block,digest)
        self.assertEqual(uploads.status(self.manager,ident)['received'],0)
        self.assertEqual(uploads.put_chunk(self.manager,ident,0,block,digest)['received'],uploads.CHUNK)

    def test_completed_background_upload_recovers_after_restart(self):
        from web.tasks import TaskRegistry
        from web.management import SQLiteManagement
        data=self.source();ident=self.init(data)['id'];self.send(ident,data)
        directory=self.root/'upload-tasks';registry=TaskRegistry(directory)
        try:
            task=registry.submit(SQLiteManagement(self.manager),'POST',['database-uploads',ident,'finalize'],{},{},'d'*32)
            done=registry.wait_initial(task['id'],None,5);self.assertEqual(done['status'],'done',done)
            signature=registry.tasks[task['id']]['fingerprint']
            interrupted=dict(done,status='running');interrupted.pop('result',None)
            registry.store.put(interrupted,signature)
        finally:registry.close()
        registry=TaskRegistry(directory,self.manager)
        try:self.assertEqual(registry.get(task['id'],None)['status'],'done')
        finally:registry.close()

    def test_http_binary_validation_origins_and_error_logging(self):
        from web.sqlite_server import create_sqlite_server
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        server=create_sqlite_server('127.0.0.1',0,data_dir=self.manager.data_dir,schema_dir=self.manager.schema_dir)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        origin=f'http://127.0.0.1:{server.server_port}'
        def call(path,method='GET',data=None,headers=None,expected=200):
            try:response=urlopen(Request(origin+'/api/manage/'+path,method=method,data=data,headers=headers or {}),timeout=10)
            except HTTPError as error:response=error
            with response:
                self.assertEqual(response.status,expected);return json.load(response)
        try:
            data=self.source();payload=dict(id='f'*32,filename='upload.sqlite3',size=len(data))
            session=call('database-uploads','POST',json.dumps(payload).encode(),{'Content-Type':'application/json'})
            block=data[:uploads.CHUNK];path='database-uploads/'+session['id']+'/chunk?offset=0'
            headers={'Content-Type':'application/octet-stream','X-Chunk-SHA256':hashlib.sha256(block).hexdigest()}
            # These guards reject headers before reading any body. Sending a
            # large unread body can legitimately reset TCP on Windows and hide
            # the HTTP response; exercise early rejection without that race.
            call(path,'PUT',b'',{**headers,'Origin':'https://wrong-origin.example'},403)
            call(path,'PUT',b'',{**headers,'Content-Type':'application/json'},415)
            call(path,'PUT',b'x',{**headers,'Content-Length':str(uploads.CHUNK+1)},413)
            call(path,'PUT',block,{**headers,'X-Chunk-SHA256':'0'*64},422)
            self.assertEqual(call('database-uploads/'+session['id'])['received'],0)
            self.assertEqual(call(path,'PUT',block,headers)['received'],uploads.CHUNK)
            self.send(session['id'],data,uploads.CHUNK)
            final=call('database-uploads/'+session['id']+'/finalize','POST',b'{}',{'Content-Type':'application/json'})
            self.assertEqual(final['bytes'],len(data))
            logs=server.sqlite_manager.audit_log.read()
            self.assertTrue(any(row['operation']=='上传分块失败' and row['status']=='failure' for row in logs))
            self.assertTrue(any(row['operation']=='校验并接收上传文件' and row['status']=='success' for row in logs))
        finally:server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':unittest.main()
