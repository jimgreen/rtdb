"""Application-owned SQLite tables use their live data and embedded layout."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from core import SQLiteDatabase, SchemaError, TableDefinition
from core.sqlite_db import SQLiteStorageError
from core.sqlite_schema import write_embedded_definition
from web.errors import APIError
from web.management import SQLiteManagement
from web.server import create_server
from web.sqlite_service import SQLiteManager
from web import test_workspace


class ScadaNativeSQLiteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.schema_dir = self.root / 'definitions'
        self.path = self.root / 'scdfe_c.sqlite3'
        self.columns = [self._column('rtu_id', 'int', True), self._column('pnt_no', 'int', True),
                        self._column('value', 'float'), self._column('payload', 'blob')]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('''CREATE TABLE fes_yc_define (
                rtu_id INTEGER NOT NULL, pnt_no INTEGER NOT NULL, value REAL CHECK (value >= 0), payload BLOB,
                PRIMARY KEY (rtu_id, pnt_no)) WITHOUT ROWID''')
            connection.execute('INSERT INTO fes_yc_define VALUES (1, 7, 12.5, ?)', (b'\x00\xff',))
            connection.execute('CREATE INDEX fes_yc_value ON fes_yc_define (value)')
            connection.execute('CREATE TABLE application_audit (value REAL)')
            connection.execute('''CREATE TRIGGER log_value AFTER UPDATE OF value ON fes_yc_define BEGIN
                INSERT INTO application_audit VALUES (NEW.value);
            END''')
            self._embed(connection)
            connection.execute("ALTER TABLE rtdb_schema_database ADD COLUMN storage_format TEXT NOT NULL DEFAULT 'native'")

    @staticmethod
    def _column(name, kind, key=False):
        return dict(eng_name=name, chn_name=name, data_type=kind, primary_key=key,
                    disp_type='文本', disp_menu='')

    def _embed(self, connection):
        definition = TableDefinition.from_mapping({
            'head': {'eng_name': 'fes_yc_define', 'chn_name': '遥测'},
            'columns': self.columns,
            'indexes': [],
        }, self.schema_dir)
        write_embedded_definition(connection, definition, 'scdfe_c')

    def test_reads_native_rows_and_reopens_after_layout_change(self):
        with SQLiteDatabase(self.root, schema_directory=self.schema_dir, database_file=self.path.name) as database:
            table = database['fes_yc_define']
            self.assertEqual(table.get_by_pk((1, 7)),
                             {'rtu_id': 1, 'pnt_no': 7, 'value': 12.5, 'payload': b'\x00\xff'})
            with self.assertRaises(SQLiteStorageError):
                table.update_by_pk((1, 7), {'value': 88})
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute('UPDATE fes_yc_define SET value=24')
            self.assertEqual(table.get_by_pk((1, 7))['value'], 24)
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('ALTER TABLE fes_yc_define ADD COLUMN quality INTEGER DEFAULT 128')
                self.columns.append(self._column('quality', 'int'))
                self._embed(connection)
            with self.assertRaises(SchemaError):
                table.all()
            database.refresh_schema()
            self.assertEqual(database['fes_yc_define'].get_by_pk((1, 7))['quality'], 128)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'rtdb_data_%' OR name LIKE 'rtdb_metadata_%'"
            ).fetchone())

    def test_web_applies_native_cell_edits_and_checks_record_versions(self):
        self.server = create_server('127.0.0.1', 0, workspace_dir=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(test_workspace.WorkspaceWebTests.stop_server, self)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        call = self.call
        database = self.path.name
        table = 'tables/fes_yc_define'
        picked = call('database-files/select', method='POST', body={'filename': database})
        self.assertFalse(picked['file']['read_only'])
        self.assertFalse(call('tables', database=database)['tables'][0]['read_only'])
        query = call(table + '/sql', method='POST', database=database,
                     body={'sql': 'SELECT * FROM fes_yc_define'})
        self.assertTrue(query['editable'])
        row = query['records'][0]
        self.assertEqual(row['row']['payload'], '00ff')
        self.assertEqual(row['ref']['key'], [1, 7])

        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE fes_yc_define SET value=20 WHERE rtu_id=1 AND pnt_no=7')
        insert = {'rtu_id': 2, 'pnt_no': 7, 'value': 42.0}
        stale = {'changes': [{'ref': row['ref'], 'values': {'value': 25}}], 'inserts': [insert]}
        conflict = call(table + '/cell-edits', database=database, method='PUT', body=stale, status=409)
        self.assertEqual(conflict['details']['conflicts'][0]['current']['payload'], '00ff')
        rows = call(table + '/rows', database=database)
        self.assertEqual(rows['total'], 1)
        row = rows['records'][0]
        self.assertEqual(row['row']['value'], 20)

        payload = {'changes': [{'ref': row['ref'], 'values': {'value': 25}}], 'inserts': [insert]}
        request_id = 'a' * 32
        edits = call(table + '/cell-edits', database=database, method='PUT', body=payload,
                     status=(200, 202), background=1, request_id=request_id)
        if 'task' in edits:
            task = edits['task']
            for _ in range(8):
                if task['status'] not in ('queued', 'running'):
                    break
                task = call('tasks/' + request_id, database=database, wait=1, revision=task['revision'])
            self.assertEqual(task['status'], 'done', task)
            edits = task['result']
        self.assertEqual((edits['updated'], edits['inserted']), (1, 1))
        self.assertEqual(call(table + '/cell-edits', database=database, method='PUT', body=payload,
                              background=1, request_id=request_id), edits)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT * FROM fes_yc_define ORDER BY rtu_id').fetchall(),
                             [(1, 7, 25, b'\x00\xff'), (2, 7, 42, None)])
            self.assertEqual(connection.execute('SELECT value FROM application_audit').fetchall(), [(20,), (25,)])

    call = test_workspace.WorkspaceWebTests.call

    def test_native_schema_edit_syncs_metadata_and_preserves_constraints(self):
        manager = SQLiteManager(self.root, self.schema_dir)
        self.addCleanup(manager.close)
        scoped = manager.for_database(self.path.name)
        management = SQLiteManagement(scoped)
        route = ['tables', 'fes_yc_define', 'schema']
        exists = management.dispatch('GET', route, {}, None)
        desired = exists['definition']
        desired['columns'][2]['eng_name'] = 'reading'
        desired['columns'].append({**self._column('quality', 'int'), 'default_value': 128})
        payload = {'definition': desired, 'version': exists['version'], 'column_renames': {'value': 'reading'}}
        preview = management.dispatch('POST', ['tables', 'fes_yc_define', 'schema-preview'], {}, payload)
        self.assertTrue(preview['valid'], preview['errors'])
        self.assertEqual(management.dispatch('GET', route, {}, None)['version'], exists['version'])
        changed = management.dispatch('PUT', route, {}, payload)
        reopened = management.dispatch('GET', route, {}, None)
        self.assertEqual(reopened['version'], changed['schema_version'])
        self.assertEqual([field['eng_name'] for field in reopened['definition']['columns']],
                         ['rtu_id', 'pnt_no', 'reading', 'payload', 'quality'])
        with self.assertRaises(APIError) as stale:
            management.dispatch('PUT', route, {}, payload)
        self.assertEqual(stale.exception.status, 409)

        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute('SELECT storage_format FROM rtdb_schema_database').fetchone(), ('native',))
            self.assertEqual(connection.execute('SELECT reading, quality FROM fes_yc_define').fetchone(), (12.5, 128))
            self.assertEqual(connection.execute('PRAGMA index_info(fes_yc_value)').fetchone()[2], 'reading')
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute('UPDATE fes_yc_define SET reading=-1')
            connection.execute('UPDATE fes_yc_define SET reading=14')
            self.assertEqual(connection.execute('SELECT value FROM application_audit').fetchone(), (14,))
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name GLOB 'rtdb_data_*'").fetchone())
        with SQLiteDatabase(self.root, schema_directory=self.schema_dir, database_file=self.path.name) as reopened_database:
            record = reopened_database['fes_yc_define'].get_by_pk((1, 7))
            self.assertEqual((record['reading'], record['quality']), (14, 128))

    def test_native_schema_refuses_drop_of_field_with_index_and_trigger(self):
        manager = SQLiteManager(self.root, self.schema_dir)
        self.addCleanup(manager.close)
        scoped = manager.for_database(self.path.name)
        management = SQLiteManagement(scoped)
        route = ['tables', 'fes_yc_define', 'schema']
        existing = management.dispatch('GET', route, {}, None)
        desired = {**existing['definition'], 'columns': [
            field for field in existing['definition']['columns'] if field['eng_name'] != 'value'
        ]}
        with self.assertRaises(APIError) as refused:
            management.dispatch('PUT', route, {}, {'definition': desired, 'version': existing['version']})
        self.assertIn(refused.exception.status, (409, 422))
        with self.assertRaises(APIError) as repair:
            scoped.rebuild_from_embedded_definition({'filename': self.path.name})
        self.assertEqual(repair.exception.status, 403)
        self.assertEqual(management.dispatch('GET', route, {}, None), existing)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT value FROM fes_yc_define').fetchone(), (12.5,))
            self.assertEqual(connection.execute('PRAGMA index_info(fes_yc_value)').fetchone()[2], 'value')

    def test_dotted_scada_database_filename_round_trips_through_manager(self):
        self.path = self.path.rename(self.root / 'scdfe_s.scdfe_s.connections.sqlite3')
        manager = SQLiteManager(self.root, self.schema_dir)
        self.addCleanup(manager.close)
        self.assertTrue(manager.select_database_file({'filename': self.path.name})['selected'])
        scoped = manager.for_database(self.path.name)
        self.assertEqual(scoped.rows('fes_yc_define')['rows'][0]['value'], 12.5)
        self.assertEqual([item['filename'] for item in manager.databases()['databases']], [self.path.name])

        management = SQLiteManagement(scoped)
        route = ['tables', 'operator_notes', 'schema']
        definition = {
            'head': {'eng_name': 'operator_notes', 'chn_name': '运维备注'},
            'columns': [self._column('id', 'int', True), self._column('note', 'str')],
            'indexes': [],
        }
        created = management.dispatch('PUT', route, {}, {'definition': definition, 'creating': True})
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM operator_notes').fetchone(), (0,))
        dropped = management.dispatch('DELETE', route, {}, {
            'confirm': 'operator_notes', 'version': created['schema_version'],
        })
        self.assertTrue(dropped['deleted'])
        self.assertEqual(dropped['remaining_tables'], ['fes_yc_define'])
        self.assertTrue((Path(dropped['archive']) / self.path.name).is_file())
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_schema WHERE name='operator_notes'").fetchone())
            self.assertEqual(connection.execute('SELECT value FROM fes_yc_define').fetchone(), (12.5,))

    def test_native_schema_edit_type_default_and_primary_key_rebuild(self):
        """原生 SQLite 表支持安全修改字段类型、默认值与主键组合，并保留索引与触发器。"""
        manager = SQLiteManager(self.root, self.schema_dir)
        self.addCleanup(manager.close)
        scoped = manager.for_database(self.path.name)
        management = SQLiteManagement(scoped)
        route = ['tables', 'fes_yc_define', 'schema']

        # 1) 修改字段类型 float -> str 并设置默认值，验证重建保留索引与触发器
        exists = management.dispatch('GET', route, {}, None)
        desired = exists['definition']
        desired['columns'][2]['data_type'] = 'str'
        desired['columns'][2]['default_value'] = '0.0'
        payload = {'definition': desired, 'version': exists['version']}
        preview = management.dispatch('POST', ['tables', 'fes_yc_define', 'schema-preview'], {}, payload)
        self.assertTrue(preview['valid'], preview['errors'])
        self.assertTrue(any('修改字段类型' in item for item in preview['changes']))
        saved = management.dispatch('PUT', route, {}, payload)
        self.assertEqual(saved['definition']['columns'][2]['data_type'], 'str')

        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute('SELECT value FROM fes_yc_define').fetchone(), ('12.5',))
            self.assertEqual(connection.execute('PRAGMA index_info(fes_yc_value)').fetchone()[2], 'value')
            self.assertEqual(connection.execute('SELECT storage_format FROM rtdb_schema_database').fetchone(), ('native',))
            # 验证新默认值生效且触发器仍正常工作
            connection.execute('INSERT INTO fes_yc_define (rtu_id, pnt_no) VALUES (2, 7)')
            self.assertEqual(connection.execute('SELECT value FROM fes_yc_define WHERE rtu_id=2').fetchone(), ('0.0',))
            connection.execute("UPDATE fes_yc_define SET value='9.5' WHERE rtu_id=2")
            self.assertEqual(connection.execute('SELECT value FROM application_audit').fetchone(), (9.5,))

        # 2) 非法类型转换（'12.5' -> int）应返回 422 且不修改原表
        bad_type = management.dispatch('GET', route, {}, None)
        bad_def = bad_type['definition']
        bad_def['columns'][2]['data_type'] = 'int'
        bad_def['columns'][2].pop('default_value', None)
        with self.assertRaises(APIError) as bad_err:
            management.dispatch('PUT', route, {}, {'definition': bad_def, 'version': bad_type['version']})
        self.assertEqual(bad_err.exception.status, 422)

        # 3) 修改主键组合：两行 pnt_no 均为 7，仅设 pnt_no 为主键应因重复被 422 拦截
        dup_pk = management.dispatch('GET', route, {}, None)
        dup_def = dup_pk['definition']
        dup_def['columns'][0]['primary_key'] = False
        dup_def['columns'][1]['primary_key'] = True
        with self.assertRaises(APIError) as dup_err:
            management.dispatch('PUT', route, {}, {'definition': dup_def, 'version': dup_pk['version']})
        self.assertEqual(dup_err.exception.status, 422)

        # 4) 合法修改主键（仅 rtu_id 为主键，两行分别为 1 和 2）并把 value 改回 float
        ok_pk = management.dispatch('GET', route, {}, None)
        ok_def = ok_pk['definition']
        ok_def['columns'][0]['primary_key'] = True
        ok_def['columns'][1]['primary_key'] = False
        ok_def['columns'][2]['data_type'] = 'float'
        ok_def['columns'][2]['default_value'] = 1.5
        updated = management.dispatch('PUT', route, {}, {'definition': ok_def, 'version': ok_pk['version']})
        self.assertEqual(updated['definition']['columns'][0]['primary_key'], True)
        self.assertEqual(updated['definition']['columns'][1]['primary_key'], False)
        self.assertEqual(updated['definition']['columns'][2]['data_type'], 'float')
        self.assertEqual(updated['definition']['columns'][2]['default_value'], 1.5)
        with SQLiteDatabase(self.root, schema_directory=self.schema_dir, database_file=self.path.name) as reopened:
            self.assertEqual(reopened['fes_yc_define'].get_by_pk(1)['value'], 12.5)
            self.assertEqual(reopened['fes_yc_define'].get_by_pk(2)['value'], 9.5)
