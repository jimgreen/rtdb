import json
from pathlib import Path
import tempfile
import unittest

from core import SQLiteDatabase, SchemaRegistry, load_database_definition


def definition(name, label, key):
    return {
        'head': {'chn_name': label, 'eng_name': name},
        'columns': [
            {'chn_name': '编号', 'eng_name': key, 'primary_key': True,
             'data_type': 'int', 'disp_type': '整数', 'disp_menu': ''},
            {'chn_name': '名称', 'eng_name': 'name', 'primary_key': False,
             'data_type': 'str', 'disp_type': '文本', 'disp_menu': ''},
        ],
    }


class SQLiteMultiDatabaseTests(unittest.TestCase):
    def test_one_sqlite_and_definition_file_can_contain_multiple_tables(self):
        with tempfile.TemporaryDirectory(prefix='rtdb-sqlite-multi-') as folder:
            root = Path(folder)
            schema = root / 'def/table'
            schema.mkdir(parents=True)
            document = {
                'database': {'eng_name': 'ems', 'chn_name': 'EMS 数据库'},
                'tables': [definition('nodes', '节点表', 'id'), definition('lines', '线路表', 'line_id')],
            }
            (schema / 'ems.json').write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
            loaded = load_database_definition(schema / 'ems.json')
            self.assertEqual(loaded.table_names, ('nodes', 'lines'))
            self.assertTrue(loaded.is_grouped)
            self.assertEqual(SchemaRegistry(schema).names(), ('lines', 'nodes'))
            with SQLiteDatabase(root / 'sqlite_dat', schema_directory=schema) as db:
                nodes, lines = db['nodes'], db['lines']
                nodes.insert_one({'id': 1, 'name': 'N1'})
                lines.insert_one({'line_id': 7, 'name': 'L7'})
                self.assertEqual(nodes.path, lines.path)
                self.assertEqual(nodes.path.name, 'ems.sqlite3')
                self.assertEqual(nodes.all(), [{'id': 1, 'name': 'N1'}])
                self.assertEqual(lines.all(), [{'line_id': 7, 'name': 'L7'}])
                connection = nodes._connection()
                names = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'" )}
                self.assertTrue({'rtdb_data_nodes', 'rtdb_data_lines',
                                 'rtdb_metadata_nodes', 'rtdb_metadata_lines'} <= names)
            with SQLiteDatabase(root / 'sqlite_dat', schema_directory=schema) as db:
                self.assertEqual(db['nodes'].get_by_pk(1)['name'], 'N1')
                self.assertEqual(db['lines'].get_by_pk(7)['name'], 'L7')


if __name__ == '__main__':
    unittest.main()
