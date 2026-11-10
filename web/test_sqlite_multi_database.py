import base64
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from web.sqlite_service import SQLiteManager, fingerprint
from web.management import SQLiteManagement
from web.definition_import import DefinitionImporter
from web.errors import APIError
from core import SQLiteDatabase


def table_definition(name, label, key):
    return {
        'head': {'chn_name': label, 'eng_name': name},
        'columns': [
            {'chn_name': '编号', 'eng_name': key, 'primary_key': True,
             'data_type': 'int', 'disp_type': '整数', 'disp_menu': ''},
            {'chn_name': '名称', 'eng_name': 'name', 'primary_key': False,
             'data_type': 'str', 'disp_type': '文本', 'disp_menu': ''},
        ],
    }


class SQLiteManagementMultiDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sqlite-web-multi-')
        root = Path(self.temp.name)
        self.data = root / 'sqlite_dat'
        self.schema = root / 'def' / 'table'
        self.schema.mkdir(parents=True)
        self.document = {
            'database': {'eng_name': 'ems', 'chn_name': 'EMS 数据库'},
            'tables': [table_definition('nodes', '节点表', 'id'),
                       table_definition('lines', '线路表', 'line_id')],
        }
        (self.schema / 'ems.json').write_text(json.dumps(self.document, ensure_ascii=False), encoding='utf-8')
        self.manager = SQLiteManager(self.data, self.schema)
        self.api = SQLiteManagement(self.manager)

    def tearDown(self):
        self.temp.cleanup()

    def test_tables_crud_schema_rename_and_shared_file(self):
        with self.manager.session() as db:
            db['nodes'].insert_one({'id': 1, 'name': 'N1'})
            db['lines'].insert_one({'line_id': 7, 'name': 'L7'})
        tables = self.manager.tables()['tables']
        self.assertEqual({item['name'] for item in tables}, {'nodes', 'lines'})
        self.assertEqual({item['database_file'] for item in tables}, {'ems.sqlite3'})
        self.assertEqual(self.api.rows('nodes', {})['table_total'], 1)
        self.assertEqual(self.api.rows('lines', {})['table_total'], 1)

        desired = copy.deepcopy(self.manager.definition('nodes').to_mapping())
        desired['head']['chn_name'] = '节点信息'
        desired['columns'].append({'chn_name': '备注', 'eng_name': 'note', 'primary_key': False,
                                   'data_type': 'str', 'disp_type': '文本', 'disp_menu': ''})
        result = self.manager.save_schema('nodes', {'definition': desired,
                                                     'version': fingerprint(self.manager.definition('nodes').to_mapping())})
        self.assertEqual(result['name'], 'nodes')
        self.assertEqual(self.api.rows('lines', {})['records'][0]['row']['name'], 'L7')
        self.assertEqual(self.api.rows('nodes', {})['records'][0]['row']['note'], None)

        renamed = copy.deepcopy(desired)
        renamed['head']['eng_name'] = 'buses'
        self.manager.save_schema('nodes', {'definition': renamed,
                                           'version': fingerprint(desired)})
        self.assertEqual(set(self.manager.tables()['tables'][0]['name'] for _ in [0]), {'buses'})
        with SQLiteDatabase(self.data, schema_directory=self.schema) as db:
            self.assertEqual(db['buses'].get_by_pk(1)['name'], 'N1')
            self.assertEqual(db['lines'].get_by_pk(7)['name'], 'L7')

        importer = DefinitionImporter(self.manager)
        text = '<buses>\n@ id name\n// 编号 名称\n# 1 N1-new\n</buses>\n'
        preview = importer.run({'text': text, 'format': 'e', 'mode': 'merge', 'source_name': 'nodes.e'}, preview=True)
        result = importer.run({'text': text, 'format': 'e', 'mode': 'merge', 'source_name': 'nodes.e',
                               'token': preview['token']}, preview=False)
        self.assertTrue(result['ok'])
        with SQLiteDatabase(self.data, schema_directory=self.schema) as db:
            self.assertEqual(db['buses'].get_by_pk(1)['name'], 'N1-new')

        (self.schema / 'ems.json').unlink()
        tables = self.manager.tables()['tables']
        self.assertEqual({item['name'] for item in tables}, {'buses', 'lines'})
        self.assertTrue(all(item['orphan'] is False for item in tables))
        self.assertEqual(self.api.rows('buses', {})['records'][0]['row']['name'], 'N1-new')
        desired = copy.deepcopy(self.manager.definition('buses').to_mapping())
        desired['head']['chn_name'] = '内置定义'
        result = self.manager.save_schema('buses', {
            'definition': desired,
            'version': fingerprint(self.manager.definition('buses').to_mapping()),
        })
        self.assertEqual(result['name'], 'buses')
        self.assertTrue((self.schema / 'ems.json').is_file())

    def test_legacy_sqlite_file_is_visible_and_collision_is_explicit(self):
        path = self.data / 'devices.sqlite3'
        connection = sqlite3.connect(path)
        try:
            connection.execute('CREATE TABLE data (id INTEGER)')
            connection.execute('INSERT INTO data VALUES (1)')
        finally:
            connection.close()
        item = next(item for item in self.manager.tables()['tables'] if item['name'] == 'devices')
        self.assertTrue(item['orphan'])
        self.assertFalse(item['selectable'])
        self.assertIn('缺少 devices.sqlite3 内置结构定义', item['error'])
        with self.assertRaises(APIError) as context:
            self.manager.save_schema('devices', {'definition': table_definition('devices', '设备表', 'id')}, creating=True)
        self.assertEqual(context.exception.status, 409)
        self.assertIn('数据库文件已存在但缺少可用的内置结构定义', context.exception.message)

    def test_database_file_snapshot_and_grouped_upload_validation(self):
        with self.manager.session() as db:
            db['nodes'].insert_one({'id': 1, 'name': 'N1'})
            db['lines'].insert_one({'line_id': 7, 'name': 'L7'})
        snapshot = self.manager.database_files()['files'][0]
        self.assertTrue(snapshot['selectable'])
        self.assertEqual({item['name'] for item in snapshot['tables']}, {'nodes', 'lines'})
        files = []
        for path in [self.data / 'ems.sqlite3', self.schema / 'ems.json']:
            files.append({'name': path.name, 'content': base64.b64encode(path.read_bytes()).decode('ascii')})
        result = self.manager.upload_database_file({'files': files, 'confirm': 'ems'})
        self.assertEqual({item['name'] for item in result['tables']}, {'nodes', 'lines'})

    def test_embedded_database_file_can_be_uploaded_without_json(self):
        with self.manager.session() as db:
            db['nodes'].insert_one({'id': 1, 'name': 'N1'})
            db['lines'].insert_one({'line_id': 7, 'name': 'L7'})
        (self.schema / 'ems.json').unlink()
        snapshot = self.manager.database_files()['files'][0]
        self.assertTrue(snapshot['selectable'])
        self.assertTrue(snapshot['embedded_definition_exists'])
        result = self.manager.upload_database_file({
            'files': [{'name': 'ems.sqlite3', 'content': base64.b64encode(
                (self.data / 'ems.sqlite3').read_bytes()).decode('ascii')}],
            'confirm': 'ems',
        })
        self.assertTrue(result['embedded_definition_used'])
        self.assertFalse(result['definition_updated'])
        self.assertFalse((self.schema / 'ems.json').exists())
        self.assertEqual(self.api.rows('nodes', {})['records'][0]['row']['name'], 'N1')

    def test_embedded_upload_ignores_stale_server_json(self):
        with self.manager.session() as db:
            db['nodes'].insert_one({'id': 1, 'name': 'N1'})
        (self.schema / 'ems.json').write_text('{ invalid json', encoding='utf-8')
        result = self.manager.upload_database_file({
            'files': [{'name': 'ems.sqlite3', 'content': base64.b64encode(
                (self.data / 'ems.sqlite3').read_bytes()).decode('ascii')}],
            'confirm': 'ems',
        })
        self.assertTrue(result['embedded_definition_used'])
        self.assertFalse(result['definition_updated'])
        self.assertEqual(self.api.rows('nodes', {})['records'][0]['row']['name'], 'N1')

    def test_embedded_definition_is_authoritative_when_json_is_stale(self):
        with self.manager.session() as db:
            db['nodes'].insert_one({'id': 1, 'name': 'N1'})
        (self.schema / 'ems.json').write_text('{ invalid json', encoding='utf-8')
        snapshot = next(item for item in self.manager.database_files()['files'] if item['name'] == 'ems')
        self.assertTrue(snapshot['selectable'])
        self.assertEqual(snapshot['definition_source'], 'SQLite 内置结构')
        self.assertEqual(self.api.rows('nodes', {})['records'][0]['row']['name'], 'N1')

    def test_embedded_only_table_can_be_deleted_without_json_sidecars(self):
        with self.manager.session() as db:
            db['nodes']
        (self.schema / 'ems.json').unlink()
        version = fingerprint(self.manager.definition('nodes').to_mapping())
        result = self.manager.drop('nodes', {'confirm': 'nodes', 'version': version})
        self.assertTrue(result['deleted'])
        self.assertFalse((self.data / 'ems.sqlite3').exists())

    def test_multi_table_e_import_creates_one_bundle(self):
        (self.schema / 'ems.json').unlink()
        text = '''<nodes>\n@ id name\n// 编号 名称\n# 1 N1\n</nodes>\n<lines>\n@ line_id name\n// 线路 名称\n# 7 L7\n</lines>\n'''
        importer = DefinitionImporter(self.manager)
        preview = importer.run({'text': text, 'format': 'e', 'mode': 'merge', 'source_name': 'ems.e'}, preview=True)
        self.assertEqual(len(preview['tables']), 2)
        result = importer.run({'text': text, 'format': 'e', 'mode': 'merge', 'source_name': 'ems.e',
                               'token': preview['token']}, preview=False)
        self.assertTrue(result['ok'])
        self.assertTrue((self.schema / 'ems.json').is_file())
        self.assertTrue((self.schema / 'ems.py').is_file())
        with SQLiteDatabase(self.data, schema_directory=self.schema) as database:
            self.assertEqual(database['nodes'].get_by_pk(1)['name'], 'N1')
            self.assertEqual(database['lines'].get_by_pk(7)['name'], 'L7')

    def test_multi_table_e_import_without_source_name_still_uses_one_bundle(self):
        (self.schema / 'ems.json').unlink()
        text = '''<nodes>\n@ id name\n// 编号 名称\n# 1 N1\n</nodes>\n<lines>\n@ line_id name\n// 线路 名称\n# 7 L7\n</lines>\n'''
        importer = DefinitionImporter(self.manager)
        preview = importer.run({'text': text, 'format': 'e', 'mode': 'merge'}, preview=True)
        self.assertEqual({item['database_file'] for item in preview['tables']}, {'nodes.sqlite3'})
        self.assertEqual({Path(item['json_path']).name for item in preview['tables']}, {'nodes.json'})
        result = importer.run({'text': text, 'format': 'e', 'mode': 'merge', 'token': preview['token']}, preview=False)
        self.assertTrue(result['ok'])
        self.assertTrue((self.data / 'nodes.sqlite3').is_file())
        self.assertTrue((self.schema / 'nodes.json').is_file())
        self.assertTrue((self.schema / 'nodes.py').is_file())
        document = json.loads((self.schema / 'nodes.json').read_text(encoding='utf-8'))
        self.assertEqual({item['head']['eng_name'] for item in document['tables']}, {'nodes', 'lines'})
        with SQLiteDatabase(self.data, schema_directory=self.schema) as database:
            self.assertEqual(database['nodes'].get_by_pk(1)['name'], 'N1')
            self.assertEqual(database['lines'].get_by_pk(7)['name'], 'L7')

    def test_multi_table_e_import_updates_existing_grouped_tables(self):
        with self.manager.session() as database:
            database['nodes'].insert_one({'id': 1, 'name': 'old-node'})
            database['lines'].insert_one({'line_id': 7, 'name': 'old-line'})
        text = '''<nodes>\n@ id name\n// 编号 名称\n# 1 new-node\n</nodes>\n<lines>\n@ line_id name\n// 线路 名称\n# 7 new-line\n</lines>\n'''
        importer = DefinitionImporter(self.manager)
        preview = importer.run({'text': text, 'format': 'e', 'mode': 'merge', 'source_name': 'ems.e'}, preview=True)
        result = importer.run({'text': text, 'format': 'e', 'mode': 'merge', 'source_name': 'ems.e',
                               'token': preview['token']}, preview=False)
        self.assertTrue(result['ok'])
        with SQLiteDatabase(self.data, schema_directory=self.schema) as database:
            self.assertEqual(database['nodes'].get_by_pk(1)['name'], 'new-node')
            self.assertEqual(database['lines'].get_by_pk(7)['name'], 'new-line')

    def test_create_and_append_table_to_grouped_file(self):
        self.schema.joinpath('ems.json').unlink()
        self.manager.create_database_file({'name': 'ems', 'chn_name': 'EMS 数据库', 'tables': [
            table_definition('nodes', '节点表', 'id'), table_definition('lines', '线路表', 'line_id')
        ]})
        self.schema.joinpath('ems.json').unlink()
        self.manager.save_schema('extra', {'creating': True, 'database_file': 'ems.sqlite3', 'definition':
            table_definition('extra', '扩展表', 'id')}, creating=True)
        loaded = json.loads(self.schema.joinpath('ems.json').read_text(encoding='utf-8'))
        self.assertEqual({item['head']['eng_name'] for item in loaded['tables']}, {'nodes', 'lines', 'extra'})
        with SQLiteDatabase(self.data, schema_directory=self.schema) as database:
            database['extra'].insert_one({'id': 3, 'name': 'E3'})
            self.assertEqual(database['extra'].get_by_pk(3)['name'], 'E3')
        version = fingerprint(self.manager.definition('extra').to_mapping())
        self.manager.clear('extra', {'confirm': 'extra', 'expected_count': 1})
        result = self.manager.drop('extra', {'confirm': 'extra', 'version': version})
        self.assertTrue(result['deleted'])
        with SQLiteDatabase(self.data, schema_directory=self.schema) as database:
            self.assertEqual(database['nodes'].name, 'nodes')
            with self.assertRaises(Exception):
                database['extra']


if __name__ == '__main__':
    unittest.main()
