"""Web management of application-owned SQLite files."""

import sqlite3
from contextlib import closing

import pytest

from core import TableDefinition
from core.sqlite_schema import write_embedded_definition
from web.errors import APIError
from web.management import SQLiteManagement
from web.sqlite_service import SQLiteManager


@pytest.mark.parametrize('suffix', ('.db', '.sqlite3'))
def test_native_database_is_browsable_and_writable(tmp_path, suffix):
    manager = SQLiteManager(tmp_path / 'data', tmp_path / 'defs')
    name = 'samp_温度_2026' if suffix == '.db' else 'samp_temp_2026'
    path = manager.data_dir / (name + suffix)
    definition = TableDefinition.from_mapping({
        'head': {'eng_name': name, 'chn_name': '采样结果'},
        'columns': [
            {'eng_name': 'sample_time', 'chn_name': '采样时间', 'data_type': 'long', 'disp_type': '整数', 'primary_key': True},
            {'eng_name': 'row_key', 'chn_name': '源行', 'data_type': 'str', 'disp_type': '文本', 'primary_key': True},
            {'eng_name': 'value', 'chn_name': '值', 'data_type': 'double', 'disp_type': '浮点数', 'primary_key': False},
        ],
        'indexes': [],
    }, tmp_path, native_table=name)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f'CREATE TABLE "{name}" (sample_time INTEGER NOT NULL, row_key TEXT NOT NULL, value REAL, PRIMARY KEY(sample_time, row_key))')
        conn.execute(f'INSERT INTO "{name}" VALUES (1800000000, ?, ?)', ('A', 12.5))
        write_embedded_definition(conn, definition, name)
        conn.execute("ALTER TABLE rtdb_schema_database ADD COLUMN storage_format TEXT NOT NULL DEFAULT 'rtdb'")
        conn.execute("UPDATE rtdb_schema_database SET storage_format = 'native'")
    with closing(sqlite3.connect(manager.data_dir / 'unmanaged.db')) as conn, conn:
        conn.execute('CREATE TABLE data (x)')
    try:
        files = manager.database_files()['files']
        assert [item['filename'] for item in files] == [path.name]
        assert not files[0]['read_only'] and files[0]['row_count'] == 1
        assert manager.select_database_file({'filename': path.name})['selected']
        with pytest.raises(APIError):
            manager.for_database('unmanaged.db')

        selected = manager.for_database(path.name)
        web = SQLiteManagement(selected)
        table = selected.tables()['tables'][0]
        assert table['name'] == name and not table['read_only'] and table['row_count'] == 1
        assert web.dispatch('GET', ['tables', name, 'schema'], {}, None)['row_count'] == 1
        assert web.dispatch('GET', ['tables', name, 'rows'], {'where': 'value > 10'}, None)['total'] == 1
        rows = web.dispatch('POST', ['tables', name, 'sql'], {}, {'sql': f'SELECT * FROM "{name}"'})
        assert rows['rows'] == [{'sample_time': 1800000000, 'row_key': 'A', 'value': 12.5}]
        assert rows['editable'] is True
        assert rows['records'][0]['ref']['key'] == [1800000000, 'A']

        changed = web.dispatch('POST', ['tables', name, 'sql'], {}, {
            'sql': f"""UPDATE "{name}" SET value = value + 0.5 WHERE row_key = 'A'""", 'confirm': name,
        })
        assert changed['rowcount'] == 1
        inserted = web.dispatch('POST', ['tables', name, 'rows'], {}, {
            'rows': [{'sample_time': 1800000001, 'row_key': 'B', 'value': 1.0}],
        })
        assert inserted['inserted'] == 1
        added = web.dispatch('GET', ['tables', name, 'rows'], {'where': "row_key = 'B'"}, None)
        deleted = web.dispatch('DELETE', ['tables', name, 'rows'], {}, {'records': [added['records'][0]['ref']]})
        assert deleted['deleted'] == 1
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute(f'SELECT * FROM "{name}"').fetchall() == [(1800000000, 'A', 13.0)]
            assert connection.execute('SELECT storage_format FROM rtdb_schema_database').fetchone() == ('native',)
            assert connection.execute("SELECT name FROM sqlite_master WHERE name GLOB 'rtdb_data_*'").fetchall() == []
        assert manager.rows(name)['rows'][0]['value'] == 13.0
    finally:
        manager.close()
