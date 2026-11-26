"""Database identity and conditional refresh regressions on isolated real files."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor

from core import SchemaError
from web.errors import APIError
from web.management import SQLiteManagement
from web.sqlite_service import SQLiteManager, fingerprint
from web.test_sqlite_multi_database import table_definition


class DatabaseScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = SQLiteManager(self.root / 'data', self.root / 'defs')
        for name in ('plant_a', 'plant_b'):
            self.manager.create_database_file({'name': name, 'tables': [
                table_definition('nodes', name, 'id'), table_definition('lines', '线路', 'id')]})

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_same_named_tables_are_isolated_and_unqualified_access_fails(self):
        for filename, value in [('plant_a.db', 'A'), ('plant_b.db', 'B')]:
            scoped = self.manager.for_database(filename)
            scoped.insert('nodes', {'rows': [{'id': 1, 'name': value}]})
        def read(filename):
            with self.manager.for_database(filename).session('nodes') as table:
                return table.all()[0]['name']
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(read, ['plant_a.db', 'plant_b.db'])), ['A', 'B'])
        with self.assertRaises((APIError, SchemaError)):
            with self.manager.session('nodes'):
                pass
        self.assertEqual(len(self.manager.tables()['tables']), 4)

    def test_schema_creation_and_rename_stay_in_selected_file(self):
        a = self.manager.for_database('plant_a.db')
        b = self.manager.for_database('plant_b.db')
        added = table_definition('meters', '电表', 'id')
        a.save_schema('meters', {'definition': added}, creating=True)
        self.assertEqual({t['name'] for t in a.tables()['tables']}, {'nodes', 'lines', 'meters'})
        self.assertEqual({t['name'] for t in b.tables()['tables']}, {'nodes', 'lines'})
        desired = copy.deepcopy(a.definition('nodes').to_mapping())
        desired['head']['chn_name'] = '新名称'
        a.save_schema('nodes', {'definition': desired, 'version': fingerprint(a.definition('nodes').to_mapping())})
        self.assertEqual(b.definition('nodes').chn_name, 'plant_b')
        a.drop('meters', {'confirm': 'meters', 'version': fingerprint(added)})
        self.assertEqual(len(a.tables()['tables']), 2)

    def test_invalid_and_missing_database_are_rejected(self):
        for filename in ('../plant_a.db', 'C:/x.db', 'missing.db'):
            with self.assertRaises(APIError):
                self.manager.for_database(filename)

    def test_empty_database_survives_last_table_deletion(self):
        self.manager.create_database_file({'name': 'empty', 'tables': []})
        scoped = self.manager.for_database('empty.db')
        self.assertEqual(scoped.tables()['tables'], [])
        added = table_definition('empty', '同名表', 'id')
        scoped.save_schema('empty', {'definition': added}, creating=True)
        scoped.drop('empty', {'confirm': 'empty', 'version': fingerprint(added)})
        self.assertTrue((self.root / 'data/empty.db').is_file())
        self.assertEqual(scoped.tables()['tables'], [])
        self.assertTrue(self.manager.select_database_file({'filename': 'empty.db'})['selected'])

    def test_legacy_single_file_can_add_tables_without_losing_data(self):
        old = table_definition('legacy', '旧表', 'id')
        self.manager.save_schema('legacy', {'definition': old}, creating=True)
        scoped = self.manager.for_database('legacy.db')
        scoped.insert('legacy', {'rows': [{'id': 1, 'name': 'retained'}]})
        scoped.save_schema('nodes', {'definition': table_definition('nodes', '新表', 'id')}, creating=True)
        with scoped.session('legacy') as table:
            self.assertEqual(table.all(), [{'id': 1, 'name': 'retained'}])
            table.insert_one({'id': 2, 'name': 'counter works'})
            self.assertEqual(len(table), 2)
        self.assertEqual(len(scoped.tables()['tables']), 2)

    def test_scoped_legacy_table_rename_keeps_database_filename(self):
        old = table_definition('legacy', '旧表', 'id')
        self.manager.save_schema('legacy', {'definition': old}, creating=True)
        scoped = self.manager.for_database('legacy.db')
        scoped.insert('legacy', {'rows': [{'id': 1, 'name': 'retained'}]})
        desired = copy.deepcopy(old)
        desired['head']['eng_name'] = 'nodes'
        scoped.save_schema('legacy', {'definition': desired, 'version': fingerprint(old)})
        self.assertTrue((self.root / 'data/legacy.db').is_file())
        self.assertEqual([t['name'] for t in scoped.tables()['tables']], ['nodes'])
        with scoped.session('nodes') as table:
            self.assertEqual(table.all()[0]['name'], 'retained')
        renamed = copy.deepcopy(desired)
        renamed['head']['eng_name'] = 'plant_b'
        scoped.save_schema('nodes', {'definition':renamed, 'version':fingerprint(desired)})
        self.assertTrue((self.root / 'data/legacy.db').is_file())
        self.assertEqual([t['name'] for t in scoped.tables()['tables']], ['plant_b'])

    def test_embedded_database_does_not_resurrect_stale_json_tables(self):
        self.manager.create_database_file({'name': 'empty', 'tables': []})
        (self.root / 'defs/empty.json').write_text(json.dumps({'database': {'eng_name':'empty'},
            'tables':[table_definition('ghost', '旧定义', 'id')]}), encoding='utf-8')
        scoped = self.manager.for_database('empty.db')
        self.assertEqual(scoped.tables()['tables'], [])
        with self.assertRaises(APIError):
            with scoped.session('ghost'):
                pass

    def test_http_requests_keep_database_scope(self):
        from web.sqlite_server import create_sqlite_server
        server = create_sqlite_server('127.0.0.1', 0, data_dir=self.root / 'data', schema_dir=self.root / 'defs')
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def call(path, payload=None):
            request = Request(f'http://127.0.0.1:{server.server_port}/api/manage/' + path,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={'Content-Type':'application/json'})
            try:
                response = urlopen(request, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.loads(response.read())
        try:
            for filename, value in [('plant_a.db', 'A'), ('plant_b.db', 'B')]:
                self.manager.for_database(filename).insert('nodes', {'rows':[{'id':1, 'name':value}]})
            def query(filename):
                status, data = call('tables/nodes/sql?database='+filename, {'sql':'SELECT * FROM nodes', 'compact':True})
                self.assertEqual(status, 200, data)
                return data['records'][0]['row']['name']
            with ThreadPoolExecutor(2) as pool:
                self.assertEqual(list(pool.map(query, ['plant_a.db', 'plant_b.db'])), ['A', 'B'])
            status, result = call('tables/nodes/sql?database=plant_a.db',
                {'sql':"UPDATE nodes SET name='updated A' WHERE id=1", 'confirm':'nodes'})
            self.assertEqual(status, 200, result)
            self.assertEqual(query('plant_b.db'), 'B')
            self.assertEqual(query('plant_a.db'), 'updated A')
            self.assertNotEqual(call('tables/nodes/sql', {'sql':'SELECT * FROM nodes'})[0], 200)
            self.assertEqual(call('tables?database=missing.db')[0], 404)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

    def test_import_preview_cannot_be_applied_to_another_database(self):
        from web.definition_import import DefinitionImporter
        payload = {'format':'e', 'text':'<nodes>\n@ id name\n# 1 A\n</nodes>', 'mode':'merge'}
        a = DefinitionImporter(self.manager.for_database('plant_a.db'))
        b = DefinitionImporter(self.manager.for_database('plant_b.db'))
        token = a.run(payload, preview=True)['token']
        with self.assertRaises(APIError):
            b.run({**payload, 'token':token}, preview=False)

    def test_scoped_e_import_uses_only_selected_database(self):
        from web.definition_import import DefinitionImporter
        scoped = self.manager.for_database('plant_a.db')
        payload = {'format': 'e', 'text': '<nodes>\n@ id name\n# 1 A\n</nodes>\n<plant_b>\n@ id name\n# 2 M\n</plant_b>', 'mode': 'merge'}
        importer = DefinitionImporter(scoped)
        preview = importer.run(payload, preview=True)
        result = importer.run({**payload, 'token': preview['token']}, preview=False)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(scoped.tables()['tables']), 3)
        self.assertEqual(len(self.manager.for_database('plant_b.db').tables()['tables']), 2)

    def test_compact_conditional_and_delta_queries(self):
        scoped = self.manager.for_database('plant_a.db')
        scoped.insert('nodes', {'rows': [{'id': 1, 'name': 'A'}, {'id': 2, 'name': 'B'}]})
        api = SQLiteManagement(scoped)
        payload = {'sql': 'SELECT * FROM nodes', 'compact': True}
        first = api.sql('nodes', payload)
        self.assertNotIn('rows', first)
        unchanged = api.sql('nodes', {**payload, 'if_version': first['result_version']})
        self.assertTrue(unchanged['unchanged'])
        self.assertNotIn('records', unchanged)
        with scoped.session('nodes') as table:
            table.kv.update(1, {'name': 'changed'})
        delta = api.sql('nodes', {**payload, 'if_version': first['result_version'],
                                'known_records': [r['ref'] for r in first['records']]})
        self.assertTrue(delta['delta'])
        self.assertEqual(len(delta['records']), 1)
        self.assertEqual(delta['records'][0]['row']['name'], 'changed')
        self.assertEqual(len(delta['record_order']), 2)
        export = api.sql('nodes', payload, exporting=True)
        self.assertEqual(len(export['rows']), 2)


if __name__ == '__main__':
    unittest.main()
