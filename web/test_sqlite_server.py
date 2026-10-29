"""Real HTTP / SQLite tests; all mutations use isolated temporary directories."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core import SQLiteDatabase
from web.server import create_server
from web.sqlite_service import SQLiteManager, atomic_json


def definition(name='devices'):
    columns = [('site', '站点', 'str', True), ('id', '编号', 'int', True),
               ('name', '名称', 'str', False), ('value', '数值', 'float', False),
               ('enabled', '启用', 'bool', False)]
    return {'head': {'chn_name': '设备表', 'eng_name': name},
            'columns': [dict(eng_name=n, chn_name=c, data_type=t, primary_key=p, disp_type='文本', disp_menu='')
                        for n, c, t, p in columns]}


def record(i, **extra):
    return dict(site='A', id=i, name=f'设备{i}', value=float(i), enabled=True, **extra)


class SQLiteWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sqlite-web-test-')
        self.root = Path(self.temp.name)
        self.server = create_server('127.0.0.1', 0, root=self.root, engine='sqlite')
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.schema = self.call('POST', '/api/sqlite/schemas', definition(), status=201)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def call(self, method, path, payload=None, *, status=200, headers=None, raw=False):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = Request(self.base + path, method=method, data=data,
                          headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            self.assertEqual(response.status, status, body.decode('utf-8', errors='replace'))
            return body if raw else json.loads(body)

    def rows(self, **params):
        return self.call('GET', '/api/sqlite/tables/devices/rows?' + urlencode(params))

    def insert(self, rows):
        return self.call('POST', '/api/sqlite/tables/devices/rows', {'rows': rows}, status=201)

    def sql(self, statement, *, confirm='devices', status=200):
        return self.call('POST', '/api/sqlite/sql', dict(table='devices', sql=statement, confirm=confirm), status=status)

    def save(self, desired, *, version=None, status=200):
        return self.call('PUT', '/api/sqlite/tables/devices/schema',
                         dict(definition=desired, version=version or self.schema['schema_version']), status=status)

    def test_routes_and_static(self):
        self.assertEqual(self.call('GET', '/api/health')['engine'], 'sqlite')
        self.assertIn(b'RTDB', self.call('GET', '/', raw=True))
        self.assertIn(b'query', self.call('GET', '/static/app.js', raw=True))
        self.assertEqual(len(self.call('GET', '/api/sqlite/tables')['tables']), 1)
        self.assertEqual(self.call('GET', '/api/sqlite/tables/devices/schema'), self.schema)
        self.call('GET', '/static/../sqlite_server.py', status=404)

    def test_primary_key_crud_and_conflict(self):
        self.insert([record(1), record(2), {**record(1), 'site': 'B'}])
        page = self.rows(where="site = 'A'", order='id DESC', limit=1, offset=1)
        self.assertEqual((page['total'], page['rows'][0]['id']), (2, 1))
        edit = dict(key=['A', 1], values={'value': 25}, version=page['versions'][0])
        self.assertEqual(self.call('PUT', '/api/sqlite/tables/devices/row', edit)['updated'], 1)
        self.call('PUT', '/api/sqlite/tables/devices/row', edit, status=409)
        edit['values'] = {'id': 9}
        self.call('PUT', '/api/sqlite/tables/devices/row', edit, status=422)
        page = self.rows()
        items = [dict(key=[r['site'], r['id']], version=v) for r, v in zip(page['rows'], page['versions'])]
        wrong = copy.deepcopy(items)
        wrong[1]['version'] = 'stale'
        self.call('DELETE', '/api/sqlite/tables/devices/rows', {'records': wrong}, status=409)
        self.assertEqual(self.rows()['total'], 3)
        self.assertEqual(self.call('DELETE', '/api/sqlite/tables/devices/rows', {'records': items})['deleted'], 3)

    def test_native_sql_and_atomic_validation(self):
        self.sql("INSERT INTO devices (site,id,name,value,enabled) VALUES ('A',1,'pump',1,1),('A',2,'valve',2,0)")
        self.assertEqual(self.sql("UPDATE devices SET value=value+10 WHERE name LIKE 'p%'")['rowcount'], 1)
        self.assertEqual(self.sql('SELECT SUM(value) AS total FROM devices')['rows'], [{'total': 13.0}])
        self.assertEqual(self.sql('WITH x AS (SELECT * FROM devices) SELECT COUNT(*) AS n FROM x')['rows'], [{'n': 2}])
        self.sql('UPDATE devices SET value=0', confirm=None, status=400)
        self.sql("UPDATE devices SET name='" + 'x' * 410 + "'")
        self.sql("INSERT INTO devices (site,id) VALUES ('B',3),('A',1)", status=422)
        self.assertEqual(self.rows()['total'], 2)
        self.assertEqual(self.sql("DELETE FROM devices WHERE id IN (2,3)")['rowcount'], 1)
        self.assertEqual(self.call('GET', '/api/sqlite/tables/devices/check')['ok'], True)

    def test_sql_restrictions_do_not_mutate(self):
        self.insert([record(1)])
        for statement in ['DELETE FROM devices nonsense', 'DELETE FROM devices; DELETE FROM devices',
                          'DELETE FROM data', 'UPDATE metadata SET row_count=0',
                          'PRAGMA writable_schema=ON', 'ATTACH DATABASE \'other.db\' AS other',
                          'DROP TABLE devices', 'SELECT * FROM metadata',
                          "SELECT readfile('x')", 'DELETE FROM devices RETURNING id']:
            with self.subTest(sql=statement):
                self.sql(statement, status=400)
                self.assertEqual(self.rows()['total'], 1)
        for statement in ["INSERT OR REPLACE INTO devices (site,id) VALUES ('A',1)",
                          'UPDATE OR REPLACE devices SET id=1',
                          'UPDATE devices SET value=rtdb_written()']:
            self.sql(statement, status=400)
            self.assertTrue(self.call('GET', '/api/sqlite/tables/devices/check')['ok'])

    def test_import_export_roundtrip_and_rollback(self):
        self.insert([{**record(1), 'id': 9223372036854775807, 'name': r'\N'}, {**record(2), 'name': None}])
        exported = self.call('GET', '/api/sqlite/tables/devices/export?format=csv', raw=True).decode('utf-8-sig')
        self.assertEqual(self.rows()['rows'][-1]['id'], '9223372036854775807')
        self.call('POST', '/api/sqlite/tables/devices/clear', {'confirm': 'devices', 'expected_count': 2})
        self.call('POST', '/api/sqlite/tables/devices/import', {'format': 'csv', 'text': exported}, status=201)
        self.assertEqual(self.rows()['total'], 2)
        data = self.call('GET', '/api/sqlite/tables/devices/export?format=json', raw=True).decode()
        self.call('POST', '/api/sqlite/tables/devices/clear', {'confirm': 'devices', 'expected_count': 2})
        self.call('POST', '/api/sqlite/tables/devices/import', {'format': 'json', 'text': data}, status=201)
        self.assertEqual(self.rows()['rows'][1]['name'], r'\N')
        for text in ['site,id\nB,3\nA,2', 'site,id\nB,3\nA,nope']:
            self.call('POST', '/api/sqlite/tables/devices/import', {'format': 'csv', 'text': text}, status=422 if 'A,2' in text else 400)
            self.assertEqual(self.rows()['total'], 2)

    def test_schema_validation_migration_and_archive(self):
        self.insert([record(1)])
        desired = copy.deepcopy(self.schema['definition'])
        desired['head']['chn_name'] = '设备信息'
        self.schema = self.save(desired)
        self.insert([record(2)])
        self.insert([record(3)])
        self.save(desired, version='stale', status=409)
        desired['columns'].append(dict(chn_name='备注', eng_name='note', primary_key=False, data_type='str', disp_type='文本', disp_menu=''))
        self.save(desired, status=409)
        self.call('POST', '/api/sqlite/tables/devices/clear', {'confirm': 'devices', 'expected_count': 0}, status=409)
        self.call('POST', '/api/sqlite/tables/devices/clear', {'confirm': 'devices', 'expected_count': 3})
        self.schema = self.save(desired)
        self.insert([record(9, note='new')])
        self.assertEqual(self.rows()['rows'][0]['note'], 'new')
        drop = dict(confirm='devices', version=self.schema['schema_version'])
        self.call('DELETE', '/api/sqlite/tables/devices/table', drop, status=409)
        self.call('POST', '/api/sqlite/tables/devices/clear', {'confirm': 'devices', 'expected_count': 1})
        result = self.call('DELETE', '/api/sqlite/tables/devices/table', drop)
        self.assertTrue((Path(result['archive']) / 'devices.sqlite3').is_file())
        self.assertEqual(self.call('GET', '/api/sqlite/tables')['tables'], [])

    def test_hot_sync_and_optimistic_external_writer(self):
        self.insert([record(1)])
        with SQLiteDatabase(self.root / 'sqlite_dat', schema_directory=self.root / 'def/table') as db:
            table = db['devices']
            page = self.rows()
            table.update_by_pk(('A', 1), {'value': 77})
            self.assertEqual(self.rows()['rows'][0]['value'], 77)
            self.call('PUT', '/api/sqlite/tables/devices/row',
                      dict(key=['A', 1], values={'value': 8}, version=page['versions'][0]), status=409)
            current = self.rows()
            self.call('PUT', '/api/sqlite/tables/devices/row',
                      dict(key=['A', 1], values={'value': 88}, version=current['versions'][0]))
            self.assertEqual(table.get_by_pk(('A', 1))['value'], 88)

    def test_invalid_inputs_are_client_errors(self):
        for params in [{'limit': 'abc'}, {'offset': -1}, {'limit': 501}, {'where': 'bad syntax'}, {'order': 'missing'}]:
            self.call('GET', '/api/sqlite/tables/devices/rows?' + urlencode(params), status=400)
        self.call('POST', '/api/sqlite/schemas', {'head': {'eng_name': '../bad'}}, status=400)
        self.call('POST', '/api/sqlite/schemas', {'head': {'eng_name': 'bad'}, 'columns': []}, status=422)
        self.call('POST', '/api/sqlite/tables/devices/rows', [], status=400)
        self.call('POST', '/api/sqlite/tables/devices/import', {'format': 'csv', 'text': ''}, status=400)
        self.call('POST', '/api/sqlite/tables/devices/import', {'format': 'json', 'text': '{'}, status=400)
        self.call('POST', '/api/sqlite/tables/devices/rows', {'rows': []}, headers={'Origin': 'https://unrelated.test'}, status=403)
        self.call('GET', '/api/health', headers={'Host': 'unrelated.test'}, status=403)
        self.sql('', status=400)

    def test_schema_journal_recovery_at_json_publish_failure(self):
        manager = self.server.sqlite_manager
        desired = copy.deepcopy(self.schema['definition'])
        desired['head']['chn_name'] = '恢复后的表'
        target = manager.schema_dir / 'devices.json'
        def fail_publish(path, value):
            if path == target:
                raise OSError('simulated interrupted definition publish')
            return atomic_json(path, value)
        with patch('web.sqlite_service.atomic_json', side_effect=fail_publish):
            with self.assertRaises(OSError):
                manager.save_schema('devices', {'definition': desired, 'version': self.schema['schema_version']})
        self.assertTrue(manager._journal.exists())
        recovered = SQLiteManager(manager.data_dir, manager.schema_dir)
        self.assertEqual(recovered.definition('devices').chn_name, '恢复后的表')
        self.assertFalse(recovered._journal.exists())
        self.assertTrue(recovered.check('devices')['ok'])


if __name__ == '__main__':
    unittest.main()
