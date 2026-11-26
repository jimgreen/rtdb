"""Whole-database snapshot isolation, publication and HTTP download regressions."""
from contextlib import closing
import hashlib
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from web import test_database_scope
from web.management import SQLiteManagement
from web.errors import APIError
from web import database_snapshots as snapshots


class SnapshotTests(unittest.TestCase):
    setUp = test_database_scope.DatabaseScopeTests.setUp
    tearDown = test_database_scope.DatabaseScopeTests.tearDown

    def source(self):
        return self.manager.for_database('plant_a.db')

    def create(self):
        return snapshots.create(self.source())

    def test_wal_and_all_tables_metadata_indexes_are_preserved(self):
        manager = self.source()
        manager.insert('nodes', {'rows': [{'id': 1, 'name': '变电站'}]})
        manager.insert('lines', {'rows': [{'id': 2, 'name': '线路'}]})
        with closing(sqlite3.connect(manager.data_dir / manager.database_file)) as writer:
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('CREATE INDEX custom_names ON rtdb_data_nodes(c1)')
            writer.execute("UPDATE rtdb_data_nodes SET c1='尚在 WAL' WHERE c0=1")
            writer.commit()
            result = self.create()
            path, item = snapshots.resolve(manager, result['id'])
            self.assertEqual(item['table_count'], 2)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item['sha256'])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT c1 FROM rtdb_data_nodes').fetchone()[0], '尚在 WAL')
                self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='custom_names'").fetchone())
            self.assertFalse(path.with_name(path.name + '-wal').exists())
            self.assertEqual(snapshots.listing(manager)['snapshots'][0]['id'], result['id'])
            with self.assertRaises(APIError):
                snapshots.resolve(self.manager.for_database('plant_b.db'), result['id'])

    def test_restore_new_database_and_definitions_without_overwriting(self):
        self.source().insert('nodes', {'rows': [{'id': 1, 'name': '保存值'}]})
        item = self.create()
        result = snapshots.restore(self.source(), item['id'], {'name': 'restored', 'chn_name': '恢复副本'})
        restored = self.manager.for_database(result['filename'])
        self.assertEqual(restored.rows('nodes')['rows'][0]['name'], '保存值')
        self.assertEqual(len(restored.tables()['tables']), 2)
        document = json.loads((self.manager.schema_dir / 'restored.json').read_text(encoding='utf-8'))
        self.assertEqual(document['database'], {'eng_name': 'restored', 'chn_name': '恢复副本'})
        self.assertTrue((self.manager.schema_dir / 'restored.py').is_file())
        with self.assertRaises(APIError) as error:
            snapshots.restore(self.source(), item['id'], {'name': 'restored'})
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.source().rows('nodes')['rows'][0]['name'], '保存值')

    def test_checksum_corruption_and_invalid_names_leave_no_target(self):
        item = self.create()
        path, _ = snapshots.resolve(self.source(), item['id'])
        with path.open('r+b') as stream:
            stream.seek(-1, 2); stream.write(b'X')
        with self.assertRaises(APIError):
            snapshots.restore(self.source(), item['id'], {'name': 'broken'})
        self.assertFalse((self.manager.data_dir / 'broken.db').exists())
        for invalid in ('../x', 'rtdb_x', ''):
            with self.assertRaises(APIError):
                snapshots.restore(self.source(), item['id'], {'name': invalid})

    def test_cancel_removes_partial_snapshot_and_restore(self):
        from web.tasks import TaskCancelled
        with patch.object(snapshots, 'checkpoint', side_effect=TaskCancelled()):
            with self.assertRaises(TaskCancelled): self.create()
        self.assertEqual(snapshots.listing(self.source())['snapshots'], [])
        item = self.create()
        with patch.object(snapshots, 'checkpoint', side_effect=TaskCancelled()):
            with self.assertRaises(TaskCancelled):
                snapshots.restore(self.source(), item['id'], {'name': 'cancelled'})
        self.assertFalse((self.manager.data_dir / 'cancelled.db').exists())

    def test_missing_source_is_not_recreated_and_scope_required(self):
        source = self.source()
        (source.data_dir / source.database_file).unlink()
        with self.assertRaises(APIError): snapshots.create(source)
        self.assertFalse((source.data_dir / source.database_file).exists())
        with self.assertRaises(APIError): snapshots.create(self.manager)

    def test_snapshot_remains_accessible_after_source_loss(self):
        item = self.create()
        source = self.source(); (source.data_dir / source.database_file).unlink()
        self.assertEqual(snapshots.listing(self.manager)['snapshots'][0]['id'], item['id'])
        scoped = snapshots.scope(self.manager, 'plant_a.db')
        restored = snapshots.restore(scoped, item['id'], {'name': 'disaster_copy'})
        self.assertTrue((source.data_dir / restored['filename']).is_file())

    def test_external_writer_is_allowed_and_snapshot_stays_consistent(self):
        source = self.source()
        source.insert('nodes', {'rows':[{'id':1, 'name':'before'}]})
        source.insert('lines', {'rows':[{'id':1, 'name':'before'}]})
        changed = []
        def during_backup(phase, *args):
            if phase == '正在生成整库快照' and not changed:
                with closing(sqlite3.connect(source.data_dir / source.database_file, timeout=.5)) as writer, writer:
                    writer.execute("UPDATE rtdb_data_nodes SET c1='after'")
                    writer.execute("UPDATE rtdb_data_lines SET c1='after'")
                changed.append(True)
        with patch.object(snapshots, 'checkpoint', side_effect=during_backup): item = self.create()
        self.assertTrue(changed)
        path, _ = snapshots.resolve(source, item['id'])
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT c1 FROM rtdb_data_nodes').fetchone()[0], 'before')
            self.assertEqual(db.execute('SELECT c1 FROM rtdb_data_lines').fetchone()[0], 'before')
        self.assertEqual(source.rows('nodes')['rows'][0]['name'], 'after')

    def test_failed_definition_export_is_recovered(self):
        item = self.create()
        with patch.object(snapshots, 'atomic_text', side_effect=OSError('readonly definition folder')):
            result = snapshots.restore(self.source(), item['id'], {'name':'recover_defs'})
        self.assertIn('definition_warning', result)
        self.assertTrue((self.manager.data_dir / result['filename']).exists())
        restored = self.manager.for_database(result['filename'])
        with restored.read_scope(): pass
        self.assertTrue((self.manager.schema_dir / 'recover_defs.py').is_file())
        self.assertFalse(restored._journal.exists())

    def test_task_results_recover_from_published_files(self):
        from web.tasks import TaskRegistry
        registry = TaskRegistry(self.root/'snapshot-tasks')
        api = SQLiteManagement(self.source())
        try:
            for parts,payload,ident in [(['database-snapshots'],{},'1'*32),
                (['database-snapshots','1'*32,'restore'],{'name':'resumed'},'2'*32)]:
                task=registry.submit(api,'POST',parts,{},payload,ident)
                done=registry.wait_initial(ident,task['database'],5)
                self.assertEqual(done['status'],'done',done)
                signature=registry.tasks[ident]['fingerprint']
                stored=dict(done,status='running');stored.pop('result',None)
                registry.store.put(stored,signature)
        finally: registry.close()
        registry=TaskRegistry(self.root/'snapshot-tasks',self.manager)
        try:
            for ident in ('1'*32,'2'*32):
                self.assertEqual(registry.get(ident,'plant_a.db')['status'],'done')
            self.assertEqual(len(snapshots.listing(api.manager)['snapshots']),1)
        finally: registry.close()

    def test_empty_database_can_be_restored(self):
        self.manager.create_database_file({'name': 'empty', 'tables': []})
        source = self.manager.for_database('empty.db')
        item = snapshots.create(source)
        snapshots.restore(source, item['id'], {'name': 'empty_copy'})
        self.assertEqual(self.manager.for_database('empty_copy.db').tables()['tables'], [])

    def test_publication_failure_does_not_leave_usable_partial_snapshot(self):
        with patch.object(snapshots, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.create()
        self.assertEqual(snapshots.listing(self.source())['snapshots'], [])
        self.assertFalse(list((self.manager.data_dir / '.web-snapshots').glob('*.db')))

    def test_download_ranges_and_scoped_http(self):
        from web.sqlite_server import create_sqlite_server
        server = create_sqlite_server('127.0.0.1', 0, data_dir=self.manager.data_dir, schema_dir=self.manager.schema_dir)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            item = self.create()
            path, _ = snapshots.resolve(self.source(), item['id'])
            url = f'http://127.0.0.1:{server.server_port}/api/manage/database-snapshots/{item["id"]}/download?database=plant_a.db'
            with urlopen(url) as response:
                whole = response.read(); etag = response.headers['ETag']
                self.assertEqual(len(whole), item['bytes'])
                self.assertEqual(response.headers['Accept-Ranges'], 'bytes')
            self.assertEqual(whole, path.read_bytes())
            for header, expected in [('bytes=0-15', whole[:16]), ('bytes=-16', whole[-16:]), ('bytes=16-', whole[16:])]:
                with urlopen(Request(url, headers={'Range': header, 'If-Range': etag})) as response:
                    self.assertEqual(response.status, 206); self.assertEqual(response.read(), expected)
            with urlopen(Request(url, headers={'Range': 'bytes=0-15', 'If-Range': 'stale'})) as response:
                self.assertEqual(response.status, 200); self.assertEqual(response.read(), whole)
            for header in ('bytes=999999999-', 'bytes=0-1,3-4', 'bytes=-0', 'garbage'):
                with self.assertRaises(HTTPError) as error: urlopen(Request(url, headers={'Range': header}))
                self.assertEqual(error.exception.code, 416)
            with self.assertRaises(HTTPError) as error: urlopen(url.replace('plant_a', 'plant_b'))
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown(); server.server_close(); worker.join(3)


if __name__ == '__main__': unittest.main()
