import sqlite3
import tempfile
import unittest
from pathlib import Path

from web.sqlite_service import SQLiteManager


class DatabaseRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sqlite-web-repair-')
        root = Path(self.temp.name)
        self.data = root / 'sqlite_dat'
        self.schema = root / 'def' / 'table'
        self.data.mkdir(parents=True)
        self.schema.mkdir(parents=True)
        self.path = self.data / 'legacy.sqlite3'
        connection = sqlite3.connect(self.path)
        try:
            connection.execute('CREATE TABLE data (_pos INTEGER PRIMARY KEY AUTOINCREMENT, c0 INTEGER NOT NULL, c1 TEXT, c2 REAL)')
            connection.execute('CREATE UNIQUE INDEX primary_key ON data (c0)')
            connection.execute('CREATE INDEX idx_c1 ON data (c1)')
            connection.execute('CREATE INDEX idx_c2 ON data (c2)')
            connection.execute('CREATE TABLE metadata (version INTEGER PRIMARY KEY CHECK(version=1), fingerprint TEXT NOT NULL, row_count INTEGER NOT NULL)')
            connection.execute('INSERT INTO metadata VALUES (1, ?, 2)', ('legacy',))
            connection.execute('INSERT INTO data(c0,c1,c2) VALUES (1,?,?)', ('one', 1.5))
            connection.execute('INSERT INTO data(c0,c1,c2) VALUES (2,?,?)', ('two', 2.5))
            connection.commit()
        finally:
            connection.close()
        self.manager = SQLiteManager(self.data, self.schema)

    def tearDown(self):
        self.temp.cleanup()

    def test_generate_embedded_definition_preserves_rows_and_clears_alarm(self):
        before = self.manager.database_files()['files'][0]
        self.assertEqual(before['status'], '缺少内置结构定义')
        result = self.manager.rebuild_embedded_definition({'filename': 'legacy.sqlite3'})
        self.assertTrue(result['generated'])
        self.assertEqual(result['primary_keys'], ['c0'])
        self.assertEqual(result['row_count'], 2)
        after = self.manager.database_files()['files'][0]
        self.assertEqual(after['status'], '可用')
        self.assertTrue(after['selectable'])
        self.assertTrue((self.schema / 'legacy.json').is_file())
        self.assertTrue((self.schema / 'legacy.py').is_file())
        rows = self.manager.tables()['tables']
        self.assertEqual(rows[0]['definition']['columns'][1]['eng_name'], 'c1')
        connection = sqlite3.connect(self.path)
        try:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'rtdb_schema_%'"
            )}
        finally:
            connection.close()
        self.assertEqual(names, {'rtdb_schema_database', 'rtdb_schema_tables', 'rtdb_schema_columns'})

    def test_rebuild_from_embedded_definition_recreates_physical_table(self):
        self.manager.rebuild_embedded_definition({'filename': 'legacy.sqlite3'})
        connection = sqlite3.connect(self.path)
        try:
            connection.execute('DROP INDEX primary_key')
            connection.execute('DROP TABLE data')
            connection.execute('CREATE TABLE data (_pos INTEGER PRIMARY KEY AUTOINCREMENT, c0 TEXT, c1 INTEGER, c2 TEXT)')
            connection.execute('INSERT INTO data(c0,c1,c2) VALUES (?,?,?)', ('3', 3, '3.5'))
            connection.commit()
        finally:
            connection.close()
        result = self.manager.rebuild_from_embedded_definition({'filename': 'legacy.sqlite3'})
        self.assertTrue(result['rebuilt'])
        connection = sqlite3.connect(self.path)
        try:
            columns = connection.execute('PRAGMA table_info(data)').fetchall()
            self.assertEqual([row[2] for row in columns[1:]], ['INTEGER', 'TEXT', 'REAL'])
            self.assertEqual(connection.execute('SELECT c0,c1,c2 FROM data').fetchone(), (3, '3', 3.5))
            self.assertEqual(connection.execute('PRAGMA index_list(data)').fetchall()[0][1], 'idx_c2')
        finally:
            connection.close()
        self.assertEqual(self.manager.database_files()['files'][0]['status'], '可用')

    def test_delete_orphan_archives_the_database(self):
        result = self.manager.delete_orphan_database_file({
            'filename': 'legacy.sqlite3', 'confirm': 'legacy.sqlite3',
        })
        self.assertTrue(result['deleted'])
        self.assertFalse(self.path.exists())
        self.assertTrue((Path(result['archive']) / 'legacy.sqlite3').is_file())
        self.assertEqual(self.manager.database_files()['files'], [])

    def test_delete_requires_exact_filename_confirmation(self):
        with self.assertRaises(Exception):
            self.manager.delete_orphan_database_file({'filename': 'legacy.sqlite3', 'confirm': 'wrong.sqlite3'})
        self.assertTrue(self.path.exists())


if __name__ == '__main__':
    unittest.main()
