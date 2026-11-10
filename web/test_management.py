"""HTTP regression tests for the SQLite management workbench."""
import copy
import base64
import json
import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.parse import urlencode

from core import SQLiteDatabase
from core.sqlite_db import SQLiteTable
from web.server import create_server
from web import test_sqlite_server as sqlite_tests

definition = sqlite_tests.definition
record = sqlite_tests.record


class ManagementTests(unittest.TestCase):
    setUp = sqlite_tests.SQLiteWebTests.setUp
    tearDown = sqlite_tests.SQLiteWebTests.tearDown
    call = sqlite_tests.SQLiteWebTests.call
    insert = sqlite_tests.SQLiteWebTests.insert

    def manage(self, method, action, payload=None, **kwargs):
        return self.call(method, '/api/manage/tables/devices/' + action, payload, **kwargs)

    def page(self, **query):
        return self.manage('GET', 'rows?' + urlencode(query))

    def import_rows(self, rows, mode='append', **extra):
        payload = dict(format='json', text=json.dumps(rows), mode=mode, confirm='devices', **extra)
        return self.manage('POST', 'import', payload)

    def test_default_engine_and_management_contract(self):
        server = create_server(port=0, root=self.root / 'default')
        try:
            self.assertEqual(server.sqlite_manager.info()['engine'], 'sqlite')
        finally:
            server.server_close()
        self.assertEqual(self.call('GET', '/api/manage/info')['engine'], 'sqlite')
        self.assertEqual(self.call('GET', '/api/manage/tables')['tables'][0]['name'], 'devices')
        self.assertEqual(self.manage('GET', 'schema')['version'], self.schema['schema_version'])
        self.manage('GET', 'rows?limit=501', status=400)
        self.manage('GET', 'rows?offset=-1', status=400)
        self.manage('GET', 'rows?where=missing_column', status=400)
        self.manage('PUT', 'rows', [], status=400)

    def test_operation_audit_records_success_and_failure(self):
        self.insert([record(1)])
        self.page()
        self.manage('POST', 'rows', {'rows': [{'site': 'A'}]}, status=422)
        self.manage('PUT', 'schema', {'definition': self.schema['definition'],
                    'version': self.schema['schema_version']})
        records = self.call('GET', '/api/manage/logs?limit=500')['logs']
        self.assertTrue(records)
        self.assertTrue(all({'id', 'timestamp', 'category', 'operation', 'status',
                             'http_status', 'method', 'path', 'duration_ms'} <= set(item) for item in records))
        self.assertTrue(any(item['operation'] == '加载表数据' and item['status'] == 'success' for item in records))
        failures = [item for item in records if item['operation'] == '添加数据' and item['status'] == 'failure']
        self.assertTrue(failures)
        self.assertEqual(failures[0]['http_status'], 422)
        self.assertIn('error', failures[0])
        self.assertTrue(any(item['operation'] == '修改表结构' and item['status'] == 'success' for item in records))
        log_path = self.server.sqlite_manager.audit_log.path
        self.assertTrue(log_path.is_file())
        self.assertGreaterEqual(len(log_path.read_text(encoding='utf-8').splitlines()), len(records))

    def test_partitioned_log_history_filters_pagination_and_retention(self):
        self.insert([record(1), record(2)])
        today = datetime.now().astimezone().date().isoformat()
        dated = self.call('GET', '/api/manage/logs?' + urlencode({'date': today, 'page': 1, 'page_size': 1}))
        self.assertEqual(dated['date'], today)
        self.assertEqual(len(dated['logs']), 1)
        self.assertGreaterEqual(dated['pages'], 2)
        second = self.call('GET', '/api/manage/logs?' + urlencode({'date': today, 'page': 2, 'page_size': 1}))
        self.assertNotEqual(dated['logs'][0]['id'], second['logs'][0]['id'])
        failures = self.call('GET', '/api/manage/logs?' + urlencode({'status': 'failure', 'category': 'data'}))
        self.assertTrue(all(item['status'] == 'failure' and item['category'] == 'data' for item in failures['logs']))
        self.assertTrue(self.server.sqlite_manager.audit_log.path.is_file())

        old_path = self.server.sqlite_manager.audit_log.log_dir / '2000-01-01.jsonl'
        old_path.write_text(json.dumps({'id': 'old', 'timestamp': '2000-01-01T00:00:00+08:00'}) + '\n', encoding='utf-8')
        config = self.call('PUT', '/api/manage/log-config', {'retention_days': 1})
        self.assertEqual(config['retention_days'], 1)
        self.assertFalse(old_path.exists())
        self.assertIn(today, config['dates'])
        cleanup = self.call('POST', '/api/manage/logs/cleanup', {})
        self.assertEqual(cleanup['retention_days'], 1)
        self.assertEqual(cleanup['deleted_records'], 0)

    def test_sorted_filtered_edit_uses_composite_key(self):
        self.insert([record(1), record(2), record(3), {**record(2), 'site': 'B'}])
        page = self.page(where="site = 'A'", order='id DESC', limit=1, offset=1)
        reference = page['records'][0]['ref']
        self.assertEqual(reference['key'], ['A', 2])
        self.assertEqual((page['total'], page['table_total']), (3, 4))
        self.manage('PUT', 'rows', {'records': [reference], 'values': {'name': 'edited'}})
        rows = {tuple(item['ref']['key']): item['row'] for item in self.page()['records']}
        self.assertEqual(rows['A', 2]['name'], 'edited')
        self.assertEqual(rows['B', 2]['name'], '设备2')
        self.manage('PUT', 'rows', {'records': [reference], 'values': {'name': 'stale'}}, status=409)

    def test_batch_conflict_and_delete_roll_back(self):
        self.insert([record(1), record(2)])
        references = [item['ref'] for item in self.page()['records']]
        with SQLiteDatabase(self.root / 'sqlite_dat', schema_directory=self.root / 'def/table') as database:
            database['devices'].update_by_pk(('A', 2), {'value': 77})
            self.manage('PUT', 'rows', {'records': references, 'values': {'value': 99}}, status=409)
            self.assertEqual(database['devices'].get_by_pk(('A', 1))['value'], 1)
            self.manage('DELETE', 'rows', {'records': references}, status=409)
            self.assertEqual(len(database['devices']), 2)
        current = [item['ref'] for item in self.page()['records']]
        self.manage('PUT', 'rows', {'records': current, 'values': {'id': 8}}, status=422)
        self.manage('PUT', 'rows', {'records': current, 'values': {'value': 99}})
        self.assertTrue(all(item['row']['value'] == 99 for item in self.page()['records']))

    def test_snapshot_detects_same_count_changes(self):
        self.insert([record(1)])
        snapshot = self.manage('GET', 'snapshot')
        reference = self.page()['records'][0]['ref']
        self.manage('PUT', 'rows', {'records': [reference], 'values': {'value': 88}})
        self.manage('POST', 'clear', {'confirm': 'devices', 'version': snapshot['version']}, status=409)
        self.manage('POST', 'import', {'format': 'json', 'text': '[]', 'mode': 'replace',
                    'confirm': 'devices', 'version': snapshot['version']}, status=409)
        self.assertEqual(self.page()['records'][0]['row']['value'], 88)
        self.manage('POST', 'clear', {'confirm': 'devices', 'version': self.manage('GET', 'snapshot')['version']})
        self.assertEqual(self.page()['total'], 0)

    def test_different_cell_edits_are_one_transaction(self):
        self.insert([record(1), record(2)])
        refs = [item['ref'] for item in self.page()['records']]
        changes = [{'ref': refs[0], 'values': {'value': 12.5}}, {'ref': refs[1], 'values': {'name': 'changed'}}]
        bad = copy.deepcopy(changes)
        bad[1]['ref']['version'] = 'stale'
        self.manage('PUT', 'cell-edits', {'changes': bad}, status=409)
        self.assertEqual(self.page()['records'][0]['row']['value'], 1)
        result = self.manage('PUT', 'cell-edits', {'changes': changes})
        self.assertEqual(result['updated'], 2)
        rows = self.page()['records']
        self.assertEqual(rows[0]['row']['value'], 12.5)
        self.assertEqual(rows[1]['row']['name'], 'changed')

    def test_blank_row_drafts_and_edits_save_atomically(self):
        self.insert([record(1)])
        ref = self.page()['records'][0]['ref']
        changes = [{'ref': ref, 'values': {'value': 12.5}}]
        # Insert conflicts are detected before any existing-row update.
        self.manage('PUT', 'cell-edits', {'changes': changes, 'inserts': [record(2), record(1)]}, status=422)
        self.assertEqual([entry['row'] for entry in self.page()['records']], [record(1)])
        result = self.manage('PUT', 'cell-edits', {'changes': changes, 'inserts': [record(2), record(3)]})
        self.assertEqual((result['updated'], result['inserted']), (1, 2))
        self.assertEqual([entry['row']['value'] for entry in self.page()['records']], [12.5, 2, 3])
        logs = self.call('GET', '/api/manage/logs?limit=500')['logs']
        for status in ('success', 'failure'):
            self.assertTrue(any(item['operation'] == '添加并修改数据' and item['status'] == status for item in logs))

    def test_blank_row_save_validates_keys_types_and_versions(self):
        self.insert([record(1)])
        ref = self.page()['records'][0]['ref']
        for rows in ([record(2), record(2)], [{**record(2), 'site': ''}], [{**record(2), 'id': None}],
                     [{**record(2), 'value': 'bad'}]):
            with self.subTest(rows=rows):
                self.manage('PUT', 'cell-edits', {'inserts': rows}, status=422)
                self.assertEqual(self.page()['total'], 1)
        self.manage('PUT', 'cell-edits', {'changes': [{'ref': {**ref, 'version': 'stale'}, 'values': {'value': 99}}],
                    'inserts': [record(2)]}, status=409)
        self.manage('PUT', 'cell-edits', {'changes': [{'ref': ref, 'values': {'id': 5}}],
                    'inserts': [record(2)]}, status=422)
        self.assertEqual([entry['row'] for entry in self.page()['records']], [record(1)])
        for payload in ({}, {'changes': [], 'inserts': []}, {'inserts': None}, {'inserts': [record(2)] * 501}):
            self.manage('PUT', 'cell-edits', payload, status=400)
        # Blank non-key values remain NULL, and 64-bit integer keys remain exact.
        huge = '9223372036854775807'
        result = self.manage('PUT', 'cell-edits', {'inserts': [{'site': 'B', 'id': huge}]})
        self.assertEqual((result['updated'], result['inserted']), (0, 1))
        row = self.page(order='site DESC')['records'][0]['row']
        self.assertEqual(row, dict(site='B', id=huge, name=None, value=None, enabled=None))

    def test_save_preflight_checks_all_keys_without_writes(self):
        self.insert([record(1), record(9)])
        ref = self.page(limit=1)['records'][0]['ref']
        changes = [{'ref': ref, 'values': {'value': 99}}]
        bad_rows = [{'site': None, 'id': 2}, {'site': '  \t', 'id': 3},
                    {'site': 'B', 'id': None}, {'site': 'B', 'id': ' '}, {'site': 'B'},
                    record(2), {**record(2), 'id': '02'}, record(9)]
        with patch.object(SQLiteTable, '_change', side_effect=AssertionError('unexpected update')) as change, \
                patch.object(SQLiteTable, '_insert', side_effect=AssertionError('unexpected insert')) as insert:
            for method, action in [('POST', 'validate-edits'), ('PUT', 'cell-edits')]:
                result = self.manage(method, action, {'changes': changes, 'inserts': bad_rows}, status=422)
                message = result['error']
                for row in range(1, 6):
                    self.assertIn(f'新增第 {row} 行', message)
                self.assertIn('不能为空', message)
                self.assertIn('新增第 7 行与新增第 6 行主键重复', message)
                self.assertIn('数据库已有记录主键重复', message)
            good = {'changes': changes, 'inserts': [record(2), {**record(1), 'site': 'B'},
                                                  {'site': 'C', 'id': '9223372036854775807'},
                                                  {'site': 'C', 'id': '9223372036854775806'}]}
            self.assertEqual(self.manage('POST', 'validate-edits', good),
                             dict(ok=True, updated=1, inserted=4))
            change.assert_not_called()
            insert.assert_not_called()
        self.assertEqual([entry['row'] for entry in self.page()['records']], [record(1), record(9)])
        logs = self.call('GET', '/api/manage/logs?limit=500')['logs']
        for status in ('success', 'failure'):
            self.assertTrue(any(item['operation'] == '保存前检查' and item['status'] == status for item in logs))

    def test_save_rechecks_keys_after_successful_preflight(self):
        self.insert([record(1)])
        ref = self.page()['records'][0]['ref']
        payload = {'changes': [{'ref': ref, 'values': {'value': 99}}], 'inserts': [record(2), record(3)]}
        self.assertTrue(self.manage('POST', 'validate-edits', payload)['ok'])
        # Another writer inserts the same key between validation and saving.
        self.insert([record(3)])
        with patch.object(SQLiteTable, '_change', side_effect=AssertionError('unexpected update')) as change, \
                patch.object(SQLiteTable, '_insert', side_effect=AssertionError('unexpected insert')) as insert:
            self.manage('PUT', 'cell-edits', payload, status=422)
            change.assert_not_called()
            insert.assert_not_called()
        self.assertEqual([entry['row'] for entry in self.page()['records']], [record(1), record(3)])

    def test_sql_cannot_change_primary_keys(self):
        self.insert([record(1), record(2)])
        self.manage('POST', 'sql', {'sql': 'UPDATE devices SET id=id+10, value=99', 'confirm': 'devices'}, status=422)
        self.manage('POST', 'sql', {'sql': "UPDATE devices SET site='B' WHERE id=2", 'confirm': 'devices'}, status=422)
        self.assertEqual([item['row']['value'] for item in self.page()['records']], [1, 2])
        result = self.manage('POST', 'sql', {'sql': 'UPDATE devices SET id=id, value=99 WHERE id=1', 'confirm': 'devices'})
        self.assertEqual(result['rowcount'], 1)

    def test_import_modes_csv_large_integer_and_rollback(self):
        huge = 9223372036854775807
        self.insert([{**record(huge), 'name': r'\N'}, record(2)])
        self.assertEqual(self.page(order='id DESC')['records'][0]['ref']['key'], ['A', str(huge)])
        text = self.manage('GET', 'export?format=csv')['text']
        snapshot = self.manage('GET', 'snapshot')
        self.manage('POST', 'import', {'format': 'csv', 'text': text, 'mode': 'replace',
                    'confirm': 'devices', 'version': snapshot['version']})
        self.assertEqual(self.page(order='id DESC')['records'][0]['row']['name'], r'\N')
        self.import_rows([{**record(2), 'value': 23}, record(3)], mode='merge')
        self.assertEqual(self.page()['total'], 3)
        self.manage('POST', 'import', {'format': 'json', 'text': json.dumps([record(4), record(2)])}, status=422)
        self.assertEqual(self.page()['total'], 3)
        self.manage('POST', 'import', {'format': 'csv', 'text': 'site,id\nA,9'}, status=422)
        projected = self.manage('GET', 'export?format=csv&columns=id,name')['text']
        self.assertEqual(projected.lstrip('\ufeff').splitlines()[0], 'id,name')
        version = self.manage('GET', 'snapshot')['version']
        self.import_rows([record(8)], mode='replace', version=version)
        self.assertEqual(self.page()['records'][0]['ref']['key'], ['A', 8])

    def test_backup_restore_existing_and_new_table(self):
        self.insert([record(1)])
        self.call('POST', '/api/sqlite/schemas', definition('other'), status=201)
        self.call('POST', '/api/sqlite/tables/other/rows', {'rows': [record(7)]}, status=201)
        backup = self.call('POST', '/api/manage/backup', {'tables': ['devices', 'other']})['text']
        document = json.loads(backup)
        document['tables'][1]['table'] = 'restored'
        document['tables'][1]['definition']['head']['eng_name'] = 'restored'
        source = json.dumps(document)
        preview = self.call('POST', '/api/manage/restore-preview', {'text': source})
        versions = {item['name']: item['version'] for item in preview['tables']}
        self.assertIsNone(versions['restored'])
        result = self.call('POST', '/api/manage/restore', {'text': source, 'versions': versions, 'confirm': '恢复备份'})
        self.assertTrue(result['ok'])
        restored = self.call('GET', '/api/manage/tables/restored/rows')['records'][0]['row']
        self.assertEqual(restored['id'], 7)
        self.assertTrue(self.call('GET', '/api/manage/tables/restored/check')['ok'])

    def test_database_file_management_create_select_upload_and_validate(self):
        created = self.call('POST', '/api/manage/database-files/create',
                            {'name': 'created_file', 'chn_name': '新建文件'})
        self.assertEqual(created['filename'], 'created_file.sqlite3')
        files = self.call('GET', '/api/manage/database-files')
        created_file = next(item for item in files['files'] if item['filename'] == 'created_file.sqlite3')
        self.assertTrue(created_file['selectable'])
        self.assertEqual(self.call('POST', '/api/manage/database-files/select',
                                    {'filename': 'created_file.sqlite3'})['name'], 'created_file')

        source = self.root / 'upload-source'
        (source / 'def').mkdir(parents=True)
        source_definition = definition('uploaded_file')
        (source / 'def' / 'uploaded_file.json').write_text(
            json.dumps(source_definition, ensure_ascii=False), encoding='utf-8')
        with SQLiteDatabase(source / 'data', schema_directory=source / 'def') as database:
            database['uploaded_file'].insert_many([record(9)])
        payload = {'files': [
            {'name': 'uploaded_file.sqlite3',
             'content': base64.b64encode((source / 'data' / 'uploaded_file.sqlite3').read_bytes()).decode()},
            {'name': 'uploaded_file.json',
             'content': base64.b64encode((source / 'def' / 'uploaded_file.json').read_bytes()).decode()},
        ]}
        uploaded = self.call('POST', '/api/manage/database-files/upload', payload)
        self.assertEqual((uploaded['name'], uploaded['row_count']), ('uploaded_file', 1))
        self.assertEqual(self.call('GET', '/api/manage/tables/uploaded_file/rows')['records'][0]['row']['id'], 9)
        self.call('POST', '/api/manage/database-files/upload', payload, status=409)
        self.call('POST', '/api/manage/database-files/upload', {**payload, 'confirm': 'uploaded_file'})
        self.call('POST', '/api/manage/database-files/upload', {
            'files': [{'name': '../escape.sqlite3', 'content': payload['files'][0]['content']}]}, status=400)
        audit = self.call('GET', '/api/manage/logs?limit=500')['logs']
        self.assertTrue(any(item['operation'] == '上传并更新数据库文件' and item['status'] == 'success' for item in audit))
        self.assertTrue(any(item['operation'] == '上传并更新数据库文件' and item['status'] == 'failure' for item in audit))

    def test_restore_preflight_and_stale_version_do_not_write(self):
        self.insert([record(1)])
        source = self.call('POST', '/api/manage/backup', {'tables': ['devices']})['text']
        preview = self.call('POST', '/api/manage/restore-preview', {'text': source})
        versions = {item['name']: item['version'] for item in preview['tables']}
        self.insert([record(2)])
        self.call('POST', '/api/manage/restore', {'text': source, 'versions': versions, 'confirm': '恢复备份'}, status=409)
        document = json.loads(source)
        document['tables'][0]['rows'].append(copy.deepcopy(document['tables'][0]['rows'][0]))
        self.call('POST', '/api/manage/restore-preview', {'text': json.dumps(document)}, status=422)
        self.assertEqual(self.page()['total'], 2)

    def test_sql_projection_schema_and_archive_routes(self):
        self.insert([record(1), record(2)])
        result = self.manage('POST', 'sql', {'sql': 'SELECT SUM(value) AS total FROM devices'})
        self.assertEqual(result['rows'], [{'total': 3.0}])
        self.manage('POST', 'sql', {'sql': 'DELETE FROM devices'}, status=400)
        self.manage('POST', 'sql', {'sql': 'DELETE FROM devices', 'confirm': 'devices'})
        self.manage('DELETE', 'schema', {'confirm': 'devices', 'version': self.schema['schema_version']})
        self.assertEqual(self.call('GET', '/api/manage/tables')['tables'], [])

    def test_sql_result_pagination_projection_and_empty_columns(self):
        self.insert([record(index) for index in range(1, 604)])
        result = self.manage('POST', 'sql', {'sql': '-- query\nSELECT id, value * 2 AS doubled FROM devices;',
            'limit': 50, 'offset': 500, 'order': {'column': 'id', 'direction': 'ASC'}, 'readonly_only': True})
        self.assertEqual((result['total'], len(result['rows']), result['rows'][0]['id']), (603, 50, 501))
        self.assertEqual(result['columns'], ['id', 'doubled'])
        self.assertFalse(result['editable'])
        limited = self.manage('POST', 'sql', {'sql': 'SELECT id FROM devices ORDER BY id DESC LIMIT 7;', 'limit': 3, 'offset': 3})
        self.assertEqual((limited['total'], limited['rows'][0]['id']), (7, 600))
        empty = self.manage('POST', 'sql', {'sql': 'SELECT id AS number FROM devices WHERE 0'})
        self.assertEqual((empty['columns'], empty['rows'], empty['total']), (['number'], [], 0))
        aggregate = self.manage('POST', 'sql', {'sql': 'WITH x AS (SELECT * FROM devices) SELECT COUNT(*) AS n FROM x'})
        self.assertEqual(aggregate['rows'], [{'n': 603}])

    def test_sql_table_view_refs_and_refresh_never_writes(self):
        self.insert([record(1), record(2)])
        result = self.manage('POST', 'sql', {'sql': 'SELECT * FROM "devices";', 'limit': 1,
            'order': {'column': 'id', 'direction': 'DESC'}})
        self.assertTrue(result['editable'])
        reference = result['records'][0]['ref']
        self.assertEqual(reference['key'], ['A', 2])
        self.manage('PUT', 'rows', {'records': [reference], 'values': {'value': 8}})
        self.manage('PUT', 'rows', {'records': [reference], 'values': {'value': 9}}, status=409)
        for statement in ['DELETE FROM devices', 'UPDATE devices SET value=99',
                          "INSERT INTO devices (site,id) VALUES ('A',3)",
                          'WITH x AS (SELECT 1) DELETE FROM devices', 'SELECT * FROM devices; DELETE FROM devices']:
            with self.subTest(sql=statement):
                self.manage('POST', 'sql', {'sql': statement, 'readonly_only': True, 'confirm': 'devices'}, status=400)
        self.assertEqual(self.page()['total'], 2)
        for query in [{'limit': 501}, {'offset': -1}, {'order': {'column': 'id; DELETE FROM devices', 'direction': 'ASC'}}]:
            self.manage('POST', 'sql', {'sql': 'SELECT * FROM devices', **query}, status=400)

    def test_sql_export_uses_executed_projection_and_order(self):
        self.insert([record(1), record(2), record(3)])
        payload = {'sql': "SELECT id, name AS label FROM devices WHERE id >= 2", 'format': 'json',
            'order': {'column': 'id', 'direction': 'DESC'}}
        document = json.loads(self.manage('POST', 'sql-export', payload)['text'])
        self.assertEqual(document['rows'], [{'id': 3, 'label': '设备3'}, {'id': 2, 'label': '设备2'}])
        exported = self.manage('POST', 'sql-export', {**payload, 'format': 'csv', 'columns': ['label']})['text']
        self.assertEqual(exported.lstrip('\ufeff').splitlines(), ['label', '设备3', '设备2'])
        self.manage('POST', 'sql-export', {'sql': 'DELETE FROM devices', 'confirm': 'devices'}, status=400)
        self.assertEqual(self.page()['total'], 3)

    def test_restore_reports_partial_runtime_failure(self):
        self.insert([record(1)])
        self.call('POST', '/api/sqlite/schemas', definition('other'), status=201)
        source = self.call('POST', '/api/manage/backup', {'tables': ['devices', 'other']})['text']
        preview = self.call('POST', '/api/manage/restore-preview', {'text': source})
        versions = {item['name']: item['version'] for item in preview['tables']}
        original = self.server.management.import_data
        def fail_second(name, payload):
            if name == 'other':
                raise OSError('simulated disk error')
            return original(name, payload)
        with patch.object(self.server.management, 'import_data', side_effect=fail_second):
            result = self.call('POST', '/api/manage/restore', {'text': source, 'versions': versions, 'confirm': '恢复备份'})
        self.assertFalse(result['ok'])
        self.assertEqual([item['ok'] for item in result['results']], [True, False])


if __name__ == '__main__':
    unittest.main()
