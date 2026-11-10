"""Populated schema migration contracts; isolated HTTP/SQLite fixtures only."""
import copy
import json
import runpy
import unittest
from unittest.mock import patch

from core import SQLiteDatabase, SchemaError
from web import test_sqlite_server as fixtures
from web.sqlite_service import SQLiteManager, atomic_json, atomic_text, rebuild_schema_data


def column(name, kind='str', key=False):
    return dict(eng_name=name, chn_name=name, data_type=kind, primary_key=key, disp_type='文本', disp_menu='')


class SchemaMigrationTests(unittest.TestCase):
    setUp = fixtures.SQLiteWebTests.setUp
    tearDown = fixtures.SQLiteWebTests.tearDown
    call = fixtures.SQLiteWebTests.call
    insert = fixtures.SQLiteWebTests.insert
    rows = fixtures.SQLiteWebTests.rows
    save = fixtures.SQLiteWebTests.save
    sql = fixtures.SQLiteWebTests.sql

    def db(self):
        return SQLiteDatabase(self.root / 'sqlite_dat', schema_directory=self.root / 'def/table')

    def files(self):
        path = self.root / 'def/table/devices'
        document = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(runpy.run_path(str(path.with_suffix('.py')))['TABLE_DEFINITION'], document)
        return document

    def test_column_defaults_persist_and_initialize_new_rows_only(self):
        self.insert([fixtures.record(1)])
        desired = copy.deepcopy(self.schema['definition'])
        defaults = dict(site='B', id='9223372036854775807', name='', value=0, enabled=False)
        for item in desired['columns']:
            item['default_value'] = defaults[item['eng_name']]
        desired['columns'].append(column('payload', 'json') | {'default_value': {'list': [False, 0]}})
        self.schema = self.save(desired)
        document = self.files()
        self.assertEqual(document['columns'][1]['default_value'], 2**63 - 1)
        self.assertEqual(self.rows()['rows'], [fixtures.record(1, payload=None)])
        generated = runpy.run_path(str(self.root / 'def/table/devices.py'))['Row']
        first, second = generated(), generated()
        self.assertEqual((first.site, first.id, first.name, first.value, first.enabled),
                         ('B', 2**63 - 1, '', 0, False))
        first.payload['list'].append('changed')
        self.assertEqual(second.payload, {'list': [False, 0]})
        payload = {'inserts': [{}]}
        self.assertTrue(self.call('POST', '/api/manage/tables/devices/validate-edits', payload)['ok'])
        self.call('PUT', '/api/manage/tables/devices/cell-edits', payload)
        self.insert([dict(site='B', id=2, name=None, value=None)])
        with self.db() as db:
            table = db['devices']
            row = table.get_by_pk(('B', 2**63 - 1))
            self.assertEqual(row, dict(site='B', id=2**63 - 1, name='', value=0, enabled=False, payload={'list': [False, 0]}))
            table.insert_one(dict(site='C', id=3))
            self.assertEqual(table.get_by_pk(('C', 3))['payload'], {'list': [False, 0]})
            self.assertIsNone(table.get_by_pk(('B', 2))['name'])
            self.assertIsNone(table.get_by_pk(('B', 2))['value'])
            table.update_by_pk(('A', 1), {'name': 'edited'})
            self.assertEqual(table.get_by_pk(('A', 1))['value'], 1)
        # Defaults that would duplicate a primary key are still rejected atomically.
        self.call('POST', '/api/manage/tables/devices/validate-edits', payload, status=422)
        self.assertEqual(self.rows()['total'], 4)
        for item in desired['columns']:
            item['default_value'] = None
        self.schema = self.save(desired)
        self.assertTrue(all('default_value' not in item for item in self.files()['columns']))
        self.insert([dict(site='D', id=4)])
        with self.db() as db:
            self.assertEqual(db['devices'].get_by_pk(('D', 4)), dict(site='D', id=4, name=None, value=None, enabled=None, payload=None))
        self.assertTrue(self.call('GET', '/api/manage/tables/devices/check')['ok'])

    def test_invalid_default_schema_save_preserves_files_and_data(self):
        self.insert([fixtures.record(1)])
        original = self.files()
        for field, value in [('id', 'bad'), ('id', '9223372036854775808'), ('site', '  '),
                             ('value', 'bad'), ('enabled', 1)]:
            with self.subTest(field=field, value=value):
                desired = copy.deepcopy(original)
                next(item for item in desired['columns'] if item['eng_name'] == field)['default_value'] = value
                self.save(desired, status=422)
                self.assertEqual(self.files(), original)
                self.assertEqual(self.rows()['rows'], [fixtures.record(1)])

    def test_empty_chinese_names_default_on_schema_save_and_preserve_records(self):
        rows = [{**fixtures.record(1), 'name': 'null'}, fixtures.record(2)]
        self.insert(rows)
        desired = copy.deepcopy(self.schema['definition'])
        desired['head']['chn_name'] = None
        labels = ['null', None, '  ', r'\N', '启用状态']
        for item, label in zip(desired['columns'], labels):
            item['chn_name'] = label
        self.schema = self.save(desired)
        document = self.files()
        self.assertEqual(document['head']['chn_name'], 'devices')
        self.assertEqual([item['chn_name'] for item in document['columns']],
                         ['site', 'id', 'name', 'value', '启用状态'])
        self.assertEqual(self.rows()['rows'], rows)
        self.assertTrue(self.call('GET', '/api/manage/tables/devices/check')['ok'])

    def test_add_drop_preserves_values_positions_sequence_and_indexes(self):
        original = copy.deepcopy(self.schema['definition'])
        original['columns'].append(column('payload', 'json'))
        self.schema = self.save(original)
        self.insert([fixtures.record(1, payload={'large': 2**60, 'list': [False, None, '中文']}),
                     fixtures.record(2, payload=None), fixtures.record(3, payload={'deleted': True})])
        self.sql('DELETE FROM devices WHERE id IN (2,3)')
        desired = copy.deepcopy(original)
        desired['columns'] = [item for item in desired['columns'] if item['eng_name'] != 'name']
        desired['columns'].insert(2, column('extra', 'float'))
        with self.db() as db:
            old_table = db['devices']
            connection = old_table._connection()
            raw = connection.execute('SELECT _pos,c5 FROM data').fetchall()
            sequence = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='data'").fetchone()[0]
            self.schema = self.save(desired)
            with self.assertRaises(SchemaError):
                old_table.select()
            with self.assertRaises(SchemaError):
                old_table.update_by_pk(('A', 1), {'value': 99})
        with self.db() as db:
            table = db['devices']
            connection = table._connection()
            self.assertEqual(connection.execute('SELECT _pos,c5 FROM data').fetchall(), raw)
            expected = fixtures.record(1, payload={'large': 2**60, 'list': [False, None, '中文']})
            expected.pop('name')
            self.assertEqual(table.select(), [{**expected, 'extra': None}])
            self.assertEqual(len(table), 1)
            indexes = {row[1] for row in connection.execute('PRAGMA index_list(data)')}
            self.assertIn('primary_key', indexes)
            self.assertTrue({'idx_c0', 'idx_c1', 'idx_c2', 'idx_c3', 'idx_c4', 'idx_c5'} <= indexes)
            table.insert_one(dict(site='B', id=4, extra=8.5))
            self.assertGreater(connection.execute('SELECT MAX(_pos) FROM data').fetchone()[0], sequence)
            self.assertEqual(len(table), 2)
            with self.assertRaises(SchemaError):
                table.insert_one(dict(site='B', id=4))
            table.delete({'site': 'B', 'id': 4})
            self.assertEqual(len(table), 1)
        self.assertEqual(self.files(), desired)
        self.assertTrue(self.call('GET', '/api/manage/tables/devices/check')['ok'])

    def test_populated_schema_edit_and_table_rename_preserve_data(self):
        self.insert([fixtures.record(1), fixtures.record(2)])
        original = copy.deepcopy(self.schema['definition'])
        desired = {
            'head': {'chn_name': '设备信息', 'eng_name': 'renamed_devices'},
            'columns': [
                column('device_id', 'str', True) | {'chn_name': '设备编号'},
                column('label', 'str') | {'chn_name': '名称'},
                column('reading', 'int') | {'chn_name': '读数'},
                column('enabled', 'bool') | {'chn_name': '启用'},
                column('region', 'str') | {'chn_name': '区域'},
            ],
        }
        response = self.call('PUT', '/api/sqlite/tables/devices/schema', {
            'definition': desired,
            'version': self.schema['schema_version'],
            'column_renames': {'site': 'region', 'id': 'device_id', 'name': 'label', 'value': 'reading'},
        })
        self.assertEqual(response['name'], 'renamed_devices')
        self.assertFalse((self.root / 'def/table/devices.json').exists())
        self.assertTrue((self.root / 'def/table/renamed_devices.json').exists())
        self.assertFalse((self.root / 'sqlite_dat/devices.sqlite3').exists())
        self.assertTrue((self.root / 'sqlite_dat/renamed_devices.sqlite3').exists())
        rows = self.call('GET', '/api/sqlite/tables/renamed_devices/rows')['rows']
        self.assertEqual(rows[0]['device_id'], '1')
        self.assertEqual(rows[0]['reading'], 1)
        self.assertEqual(rows[0]['region'], 'A')
        self.call('GET', '/api/sqlite/tables/devices/schema', status=404)
        self.assertTrue(self.call('GET', '/api/sqlite/tables/renamed_devices/check')['ok'])

        bad = copy.deepcopy(desired)
        bad['head']['chn_name'] = '仍应保留'
        bad['columns'][1]['data_type'] = 'int'
        self.call('PUT', '/api/sqlite/tables/renamed_devices/schema', {
            'definition': bad, 'version': response['schema_version']}, status=422)
        self.assertEqual(self.call('GET', '/api/sqlite/tables/renamed_devices/rows')['rows'], rows)

    def test_empty_table_allows_type_key_and_order_changes_and_keeps_sequence(self):
        self.insert([fixtures.record(1), fixtures.record(2)])
        self.sql('DELETE FROM devices')
        desired = copy.deepcopy(self.schema['definition'])
        desired['columns'] = [column('name', key=True), column('id', 'str'), column('site')]
        self.schema = self.save(desired)
        with self.db() as db:
            table = db['devices']
            table.insert_one(dict(name='new', id='text', site='A'))
            self.assertGreater(table._connection().execute('SELECT _pos FROM data').fetchone()[0], 2)
        self.assertEqual(self.files(), desired)

    def test_sql_failure_rolls_back_data_schema_files_and_sequence(self):
        self.insert([fixtures.record(1)])
        before = self.files()
        desired = copy.deepcopy(before); desired['columns'].append(column('extra'))
        def fail_after_rebuild(*args):
            rebuild_schema_data(*args)
            raise RuntimeError('simulated failure after copying and rebuilding')
        manager = self.server.sqlite_manager
        with patch('web.sqlite_service.rebuild_schema_data', side_effect=fail_after_rebuild):
            with self.assertRaises(RuntimeError):
                manager.save_schema('devices', dict(definition=desired, version=self.schema['schema_version']))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.rows()['rows'], [fixtures.record(1)])
        with self.db() as db:
            connection = db['devices']._connection()
            self.assertEqual(connection.execute("SELECT seq FROM sqlite_sequence WHERE name='data'").fetchone()[0], 1)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='data_schema_next'").fetchone())
        self.assertTrue(manager.check('devices')['ok'])
        self.assertFalse(manager._journal.exists())

    def test_recover_committed_populated_migration_after_file_publish_failures(self):
        self.insert([fixtures.record(1)])
        manager = self.server.sqlite_manager
        for extension, function, original in [('json', 'atomic_json', atomic_json), ('py', 'atomic_text', atomic_text)]:
            with self.subTest(extension=extension):
                desired = copy.deepcopy(self.schema['definition'])
                desired['columns'].append(column('extra_' + extension))
                target = manager.schema_dir / ('devices.' + extension)
                def fail_publish(path, value):
                    if path == target:
                        raise OSError('simulated publish failure')
                    return original(path, value)
                with patch('web.sqlite_service.' + function, side_effect=fail_publish):
                    with self.assertRaises(OSError):
                        manager.save_schema('devices', dict(definition=desired, version=self.schema['schema_version']))
                self.assertTrue(manager._journal.exists())
                recovered = SQLiteManager(manager.data_dir, manager.schema_dir)
                self.assertEqual(self.files(), desired)
                self.assertTrue(recovered.check('devices')['ok'])
                self.assertFalse(manager._journal.exists())
                self.assertEqual(self.rows()['rows'][0]['extra_' + extension], None)
                self.schema = self.call('GET', '/api/sqlite/tables/devices/schema')

    def test_record_inserted_after_empty_schema_read_is_still_protected(self):
        self.assertEqual(self.call('GET', '/api/manage/tables/devices/schema')['row_count'], 0)
        desired = copy.deepcopy(self.schema['definition'])
        desired['columns'][3]['data_type'] = 'int'
        with self.db() as db:
            db['devices'].insert_one(fixtures.record(1))
        self.schema = self.save(desired)
        self.assertEqual(self.rows()['rows'][0]['value'], 1)


if __name__ == '__main__':
    unittest.main()
