import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .sqlite_db import SQLiteDatabase, SQLiteStorageError
from .sqlite_schema import find_embedded_table, write_embedded_definition
from .table_schema import SchemaError, TableDefinition


class NativeSQLiteTests(unittest.TestCase):
    table_name = 'samp_母线_2026'

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / f'{self.table_name}.db'
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                'CREATE TABLE "samp_母线_2026" ('
                'sample_time INTEGER NOT NULL, row_key TEXT NOT NULL, "读数" REAL, '
                'PRIMARY KEY(sample_time, row_key)) WITHOUT ROWID'
            )
            connection.executemany(
                'INSERT INTO "samp_母线_2026" VALUES (?,?,?)',
                [(10, '设备A', 235.0), (12, '设备A', '运维中')],
            )
            self.write_definition(connection)
            connection.execute("ALTER TABLE rtdb_schema_database ADD COLUMN storage_format TEXT NOT NULL DEFAULT 'rtdb'")
            connection.execute("UPDATE rtdb_schema_database SET storage_format='native'")

    def write_definition(self, connection, *, extra=False):
        column_specs = [
            ('sample_time', 'int', True),
            ('row_key', 'str', True),
            ('读数', 'float', False),
        ]
        if extra:
            column_specs.append(('remark', 'str', False))
        definition = TableDefinition.from_mapping(
            {
                'head': {'eng_name': self.table_name, 'chn_name': self.table_name},
                'columns': [
                    dict(eng_name=name, chn_name=name, primary_key=key, data_type=typ,
                         disp_type='文本', disp_menu='')
                    for name, typ, key in column_specs
                ],
            },
            self.directory, self.table_name, native_table=self.table_name,
        )
        write_embedded_definition(connection, definition, self.path.stem)

    def test_scoped_and_unscoped_reads_use_the_actual_business_table(self):
        for database_file in (self.path.name, None):
            with self.subTest(database_file=database_file), SQLiteDatabase(
                self.directory, schema_directory=self.directory / 'empty', database_file=database_file,
            ) as database:
                table = database[self.table_name]
                self.assertEqual(table.data_table_name, self.table_name)
                self.assertTrue(table.read_only)
                self.assertEqual(len(table), 2)
                self.assertEqual(table.count({'row_key': '设备A'}), 2)
                self.assertEqual(table.get_by_pk((12, '设备A'))['读数'], '运维中')
                self.assertEqual(
                    table.select(columns=['读数'], order_by=[('sample_time', 'DESC')], limit=1),
                    [{'读数': '运维中'}],
                )
                with self.assertRaises(SQLiteStorageError):
                    table.clear()
        embedded = find_embedded_table(self.directory, self.table_name, self.path.name)
        self.assertEqual(embedded[1].native_tables, {self.table_name: self.table_name})

    def test_open_native_table_requires_reopen_after_metadata_change(self):
        with SQLiteDatabase(self.directory, database_file=self.path.name) as database:
            table = database[self.table_name]
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute('ALTER TABLE "samp_母线_2026" ADD COLUMN remark TEXT')
                self.write_definition(connection, extra=True)
            with self.assertRaisesRegex(SchemaError, 'close and reopen'):
                table.all()
            database.refresh_schema()
            self.assertEqual(database[self.table_name].get_by_pk((10, '设备A'))['remark'], None)

    def test_native_opt_in_writes_physical_rows_and_retains_sql_defaults(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('ALTER TABLE "samp_母线_2026" ADD COLUMN remark TEXT DEFAULT \'native\'')
            connection.execute(
                'CREATE TABLE dependency (sample_time INTEGER, row_key TEXT, '
                'FOREIGN KEY (sample_time,row_key) REFERENCES "samp_母线_2026"(sample_time,row_key) ON DELETE RESTRICT)'
            )
            connection.execute('INSERT INTO dependency VALUES (10, ?)', ('设备A',))
            self.write_definition(connection, extra=True)

        with SQLiteDatabase(self.directory, database_file=self.path.name, native_writable=True) as database:
            table = database[self.table_name]
            self.assertTrue(table.native)
            self.assertFalse(table.read_only)
            self.assertEqual(table.update({'读数': 239}, limit=1), 1)
            self.assertEqual(table.update({'读数': 240}, lambda row: row['sample_time'] < 12), 1)
            self.assertEqual(table.get_by_pk((10, '设备A'))['读数'], 240.0)
            self.assertEqual(table.get_by_pk((12, '设备A'))['读数'], '运维中')
            self.assertEqual(table.insert_many([
                {'sample_time': 20, 'row_key': '设备B'},
                {'sample_time': 21, 'row_key': '设备B', 'remark': None},
            ]), 2)
            self.assertEqual(table.get_by_pk((20, '设备B'))['remark'], 'native')
            self.assertIsNone(table.get_by_pk((21, '设备B'))['remark'])
            self.assertEqual(table.update_by_pk((21, '设备B'), {'remark': '已改'}), 1)
            self.assertEqual(table.delete({'row_key': '设备B'}, limit=1), 1)
            self.assertEqual(table.get_by_pk((21, '设备B'))['remark'], '已改')
            self.assertEqual(table.delete_by_pk((21, '设备B')), 1)

        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM "samp_母线_2026"').fetchone()[0], 2)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM dependency').fetchone()[0], 1)
            self.assertFalse(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='metadata' OR name LIKE 'rtdb_data_%'"
            ).fetchall())

    def test_native_write_batch_rolls_back_on_constraint_failure(self):
        with SQLiteDatabase(self.directory, database_file=self.path.name, native_writable=True) as database:
            table = database[self.table_name]
            with self.assertRaises(SchemaError):
                with table.transaction() as batch:
                    batch.insert({'sample_time': 11, 'row_key': '设备A', '读数': 222})
                    batch.insert({'sample_time': 12, 'row_key': '设备A', '读数': 233})
            self.assertIsNone(table.kv.get((11, '设备A')))
            self.assertEqual(table.get_by_pk((12, '设备A'))['读数'], '运维中')

    def test_normal_db_file_cannot_be_selected(self):
        unmarked = self.directory / 'ordinary.db'
        with closing(sqlite3.connect(unmarked)) as connection:
            connection.execute('CREATE TABLE ordinary (id INTEGER PRIMARY KEY)')
        with self.assertRaisesRegex(ValueError, 'native SQLite embedded definition'):
            SQLiteDatabase(self.directory, database_file=unmarked.name)
        self.assertIsNone(find_embedded_table(self.directory, 'ordinary'))
