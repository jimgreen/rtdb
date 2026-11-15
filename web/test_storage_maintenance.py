"""Real-file cleanup boundaries and durable replay protection."""
from contextlib import closing
import json
import os
from pathlib import Path
import time
import unittest
import uuid
from unittest.mock import patch

from web import database_snapshots as snapshots, database_uploads as uploads
from web import storage_maintenance as storage
from web.errors import APIError
from web.management import SQLiteManagement
from web.task_store import TaskStore
from web import test_database_scope


class StorageTests(unittest.TestCase):
    setUp = test_database_scope.DatabaseScopeTests.setUp
    tearDown = test_database_scope.DatabaseScopeTests.tearDown

    @property
    def service(self):
        return self.manager.storage_maintenance

    def age(self, path, days=40):
        stamp = time.time()-days*86400
        os.utime(path, (stamp, stamp))

    def upload(self, state='receiving', days=40):
        ident = uuid.uuid4().hex
        uploads.create(self.manager, dict(id=ident, filename='test.sqlite3', size=10))
        folder = uploads.session_path(self.manager, ident)
        meta = folder/'state.json'
        value = json.loads(meta.read_text(encoding='utf-8'))
        value.update(state=state, updated=time.time()-days*86400)
        meta.write_text(json.dumps(value), encoding='utf-8')
        for path in folder.iterdir(): self.age(path, days)
        return folder

    def snap(self):
        item = snapshots.create(self.manager.for_database('plant_a.sqlite3'))
        root = snapshots.directory(self.manager)
        for ext in ('.sqlite3', '.json'): self.age(root/(item['id']+ext))
        return item

    def task(self, store, status='done', days=40, **extra):
        ident = uuid.uuid4().hex
        state = dict(id=ident, database=None, method='POST', path='backup', status=status,
                     created=time.time()-days*86400, phase='完成', cancellable=False,
                     completed_tables=['nodes'], result={'text':'测试'*5000}, **extra)
        store.put(state, 'fingerprint')
        with closing(store.connect()) as db, db:
            db.execute('UPDATE tasks SET updated=? WHERE id=?', (time.time()-days*86400,ident))
        return ident

    def clean(self):
        return self.service.clean({'token':self.service.preview()['token']})

    def test_stats_and_cleanup_preserve_business_database(self):
        source = self.manager.data_dir/'plant_a.sqlite3'
        before = source.read_bytes()
        folder = self.upload()
        self.manager.record_operation(category='system',operation='fixture',status='success',http_status=200,method='GET',path='/fixture')
        preview = self.service.preview()
        self.assertEqual(preview['statistics']['uploads']['candidates'], 1)
        self.assertGreater(preview['statistics']['logs']['bytes'], 0)
        result = self.service.clean({'token':preview['token']})
        self.assertEqual(result['cleaned'], 1)
        self.assertFalse(folder.exists())
        self.assertEqual(source.read_bytes(), before)

    def test_snapshot_pair_and_upload_are_independent(self):
        item = self.snap()
        folder = self.upload('ready')
        self.service.configure({**storage.DEFAULTS,'snapshot_retention_days':0})
        self.assertEqual(self.clean()['cleaned'],1)
        self.assertFalse(folder.exists())
        path = snapshots.directory(self.manager)/(item['id']+'.sqlite3')
        self.assertTrue(path.exists())
        self.service.configure(storage.DEFAULTS)
        self.assertEqual(self.clean()['cleaned'],1)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix('.json').exists())

    def test_unknown_orphan_and_validating_files_are_protected(self):
        folder = self.upload('validating')
        other = self.upload()
        (other/'do-not-delete.txt').write_text('user file')
        root = snapshots.directory(self.manager)
        orphan = root/(uuid.uuid4().hex+'.sqlite3');orphan.write_bytes(b'unknown');self.age(orphan)
        temporary = root/'.building-old';temporary.mkdir();(temporary/'snapshot.sqlite3').write_bytes(b'temp');self.age(temporary)
        preview = self.service.preview()
        self.assertFalse(preview['candidates'])
        self.assertEqual(preview['protected_count'],4)
        self.clean()
        self.assertTrue(folder.exists() and other.exists() and orphan.exists() and temporary.exists())

    def test_preview_change_rejects_all_deletion(self):
        folder = self.upload();self.snap()
        preview = self.service.preview()
        (folder/'payload.sqlite3').write_bytes(b'new data')
        with self.assertRaisesRegex(APIError, '变化'):
            self.service.clean({'token':preview['token']})
        self.assertTrue(folder.exists())
        self.assertEqual(snapshots.listing(self.manager)['total'],1)

    def test_expired_preview_and_reuse_are_rejected(self):
        folder = self.upload()
        token = self.service.preview()['token']
        with patch.object(storage.time,'monotonic',return_value=time.monotonic()+601):
            with self.assertRaisesRegex(APIError, '过期'):
                self.service.clean({'token':token})
        self.assertTrue(folder.exists())
        token = self.service.preview()['token']
        self.service.clean({'token':token})
        with self.assertRaises(APIError): self.service.clean({'token':token})

    def test_retention_validation_disable_and_configuration_invalidates_preview(self):
        self.upload()
        token=self.service.preview()['token']
        for value in (-1, True, 1.5, '2', 36501):
            with self.assertRaises(APIError): self.service.configure({**storage.DEFAULTS,'upload_retention_days':value})
        self.service.configure(dict.fromkeys(storage.DEFAULTS,0))
        with self.assertRaises(APIError):self.service.clean({'token':token})
        self.assertFalse(self.service.preview()['candidates'])
        self.assertFalse(uploads.upload_expired(self.manager,dict(state='receiving',updated=0)))

    def test_active_tasks_protect_uploads_and_snapshots(self):
        folder=self.upload();self.snap()
        store=TaskStore(self.manager.data_dir/'.web-tasks')
        try:
            token=self.service.preview()['token']
            self.task(store,'queued')
            with self.assertRaisesRegex(APIError,'变化'):self.service.clean({'token':token})
            preview=self.service.preview()
            self.assertFalse(preview['candidates'])
            self.assertEqual(preview['statistics']['tasks']['active'],1)
            self.assertTrue(folder.exists())
            self.assertFalse(uploads.upload_expired(self.manager,dict(state='receiving',updated=0)))
        finally:store.close()

    def test_compaction_retains_fingerprint_and_interrupted_task(self):
        store=TaskStore(self.manager.data_dir/'.web-tasks')
        try:
            ident=self.task(store, timings={'heavy':'x'*1000})
            interrupted=self.task(store,'interrupted')
            recent=self.task(store,days=0)
            result=self.clean()
            self.assertEqual(result['compacted_tasks'],1)
            signature,task=store.get(ident)
            self.assertEqual(signature,'fingerprint')
            self.assertTrue(task['details_purged'] and task['result_expired'])
            self.assertNotIn('timings',task)
            self.assertIn('result',store.get(interrupted)[1])
            self.assertIn('result',store.get(recent)[1])
            self.assertEqual(self.clean()['cleaned'],0)
        finally:store.close()

    def test_shared_activity_blocks_cleanup_without_mutation(self):
        folder=self.upload();token=self.service.preview()['token']
        with storage.fence(self.manager):
            with self.assertRaises(Exception):self.service.clean({'token':token})
        self.assertTrue(folder.exists())
        self.assertEqual(self.service.clean({'token':token})['cleaned'],1)

    def test_log_policy_and_files_remain_under_existing_log_manager(self):
        self.manager.audit_log.set_retention(0)
        log=self.manager.audit_log.log_dir/'2000-01-01.jsonl'
        log.write_text('{}\n')
        preview=self.service.preview()
        self.assertEqual(preview['statistics']['logs']['retention_days'],0)
        self.clean()
        self.assertTrue(log.exists())

    def test_failed_delete_is_reported_and_audited(self):
        folder=self.upload()
        api=SQLiteManagement(self.manager)
        token=api.dispatch('GET',['storage-maintenance'],{},None)['token']
        with patch.object(uploads,'_remove',side_effect=PermissionError('locked')):
            result=api.dispatch('POST',['storage-maintenance'],{},{'token':token})
        self.assertFalse(result['ok'])
        self.assertEqual(result['failures'][0]['error'],'locked')
        self.assertTrue(folder.exists())
        logs=''.join(p.read_text(encoding='utf-8') for p in self.manager.audit_log.log_dir.glob('*.jsonl'))
        self.assertIn('执行存储清理',logs)
        self.assertIn('failure',logs)

    def test_recent_file_is_not_candidate_even_when_state_is_old(self):
        folder=self.upload()
        (folder/'payload.sqlite3').write_bytes(b'new')
        self.assertFalse(self.service.preview()['candidates'])

    def test_result_auto_expiration_obeys_retention(self):
        self.service.configure({**storage.DEFAULTS,'task_retention_days':0})
        store=TaskStore(self.manager.data_dir/'.web-tasks')
        try:
            ident=self.task(store)
            with closing(store.connect()) as db,db:store.expire_results(db)
            self.assertIn('result',store.get(ident)[1])
            self.service.configure(storage.DEFAULTS)
            with closing(store.connect()) as db,db:store.expire_results(db)
            self.assertTrue(store.get(ident)[1]['result_expired'])
        finally:store.close()

    def test_task_batch_rolls_back_if_a_task_changed(self):
        store=TaskStore(self.manager.data_dir/'.web-tasks')
        try:
            first=self.task(store);second=self.task(store)
            items=self.service.preview()['candidates']
            with closing(store.connect()) as db,db:
                db.execute('UPDATE tasks SET updated=? WHERE id=?',(time.time(),second))
            with self.assertRaises(ValueError):self.service._compact_tasks(items)
            self.assertIn('result',store.get(first)[1])
            self.assertNotIn('details_purged',store.get(first)[1])
        finally:store.close()

    def test_compacted_task_cannot_replay_after_executor_restart(self):
        from types import SimpleNamespace
        from web.tasks import TaskRegistry
        calls=[]
        api=SimpleNamespace(manager=self.manager,dispatch=lambda *args:(calls.append(1) or {'ok':True}))
        directory=self.manager.data_dir/'.web-tasks'
        registry=TaskRegistry(directory)
        ident=uuid.uuid4().hex
        try:
            registry.submit(api,'POST',['backup'],{},{},ident)
            self.assertEqual(registry.wait_initial(ident,None,5)['status'],'done')
            with closing(registry.store.connect()) as db,db:
                db.execute('UPDATE tasks SET updated=? WHERE id=?',(time.time()-40*86400,ident))
            self.assertEqual(self.clean()['compacted_tasks'],1)
        finally:registry.close()
        registry=TaskRegistry(directory)
        try:
            replay=registry.submit(api,'POST',['backup'],{},{},ident)
            self.assertTrue(replay['result_expired'])
            self.assertEqual(calls,[1])
            with self.assertRaises(APIError):registry.submit(api,'POST',['backup'],{},{'different':True},ident)
        finally:registry.close()

    def test_linked_upload_and_snapshot_never_followed(self):
        outside=self.root/'outside';outside.mkdir();(outside/'state.json').write_text('{}')
        link=uploads.directory(self.manager)/uuid.uuid4().hex
        try:link.symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('directory symlink permission unavailable')
        try:
            preview=self.service.preview()
            self.assertFalse(preview['candidates'])
            self.assertGreater(preview['protected_count'],0)
            self.clean();self.assertTrue((outside/'state.json').exists())
        finally:link.unlink()


if __name__ == '__main__': unittest.main()
