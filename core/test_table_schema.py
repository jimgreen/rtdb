import json
import tempfile
import unittest
from pathlib import Path

from core import FileMappedDatabase, SchemaError, SchemaRegistry, TableDefinition, load_table_definition


class TableSchemaTests(unittest.TestCase):
    def _write_definition(self, root: Path) -> None:
        payload = {
            "head": {"chn_name": "用户表", "eng_name": "users"},
            "columns": [
                {"chn_name": "用户编号", "eng_name": "user_id", "primary_key": True, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "用户名", "eng_name": "name", "primary_key": False, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
                {"chn_name": "状态", "eng_name": "status", "primary_key": False, "data_type": "str", "disp_type": "下拉框", "disp_menu": "启用|停用"},
            ],
        }
        (root / "users.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_load_and_round_trip_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_definition(root)
            definition = load_table_definition(root, "users")
            self.assertEqual(definition.eng_name, "users")
            self.assertEqual(definition.primary_keys, ("user_id",))
            self.assertEqual(definition.column("status").data_type, "str")
            self.assertEqual(definition.column("status").disp_menu_items, ("启用", "停用"))
            self.assertEqual(SchemaRegistry(root).names(), ("users",))

            output = root / "out"
            definition.to_json(output)
            copied = TableDefinition.load(output, "users")
            self.assertEqual(copied.column_names, definition.column_names)
            self.assertEqual(copied.to_mapping(), definition.to_mapping())

    def test_definition_enforces_database_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_definition(root)
            dat = root / "dat"
            db = FileMappedDatabase(dat, schema_directory=root)
            users = db["users"]
            users.append({"user_id": 1, "name": "Alice", "status": "启用"})
            self.assertEqual(users.get_by_pk(1)["name"], "Alice")
            self.assertEqual(users.update_by_pk(1, {"name": "Alicia"}), 1)
            self.assertEqual(users.get_by_pk(1)["name"], "Alicia")
            with self.assertRaises(SchemaError):
                users.append({"user_id": 1, "name": "Duplicate", "status": "停用"})
            with self.assertRaises(SchemaError):
                users.append({"user_id": "wrong-type", "name": "Bad", "status": "停用"})
            users.extend([
                {"user_id": 2, "name": "Bob", "status": "启用"},
                {"user_id": 3, "name": "Cara", "status": "停用"},
            ])
            users.append({"user_id": 4, "name": "Dan", "status": "启用"})
            self.assertEqual(len(users), 4)
            self.assertEqual(users.delete_by_pk(1), 1)

    def test_public_definition_has_no_capacity_or_length_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_definition(root)
            definition = TableDefinition.load(root, 'users')
            mapping = definition.to_mapping()
            self.assertNotIn('max_elements', mapping['head'])
            self.assertTrue(all('data_length' not in c for c in mapping['columns']))
            definition.validate_rows([{'user_id': n, 'name': '长文本' * 2000} for n in range(10)])
            mapping['head']['max_elements'] = 5
            with self.assertRaises(SchemaError):
                TableDefinition.from_mapping(mapping, root)
            del mapping['head']['max_elements']
            mapping['columns'][0]['data_length'] = 8
            with self.assertRaises(SchemaError):
                TableDefinition.from_mapping(mapping, root)

    def test_mmap_physical_settings_persist_independently_of_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_definition(root)
            with FileMappedDatabase(root / 'dat', schema_directory=root,
                                    fixed_max_records=2, fixed_text_bytes=16) as db:
                table = db['users']
                table.append({'user_id': 1, 'name': 'one'})
                with self.assertRaises(SchemaError):
                    table.append({'user_id': 2, 'name': 'x' * 17})
                self.assertEqual(len(table), 1)
                table.append({'user_id': 2, 'name': 'two'})
                with self.assertRaises(SchemaError):
                    table.append({'user_id': 3, 'name': 'three'})
                with db.write_process() as writer:
                    writer.update_by_pk('users', 1, {'name': 'updated'})
            # Reopening with defaults must use the on-disk capacity/widths.
            with FileMappedDatabase(root / 'dat', schema_directory=root) as reopened:
                self.assertEqual(reopened['users'].get_by_pk(1)['name'], 'updated')
                self.assertEqual(reopened['users']._fixed_store.max_records, 2)

    def test_database_discovers_default_sibling_definition_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema_root = root / "def" / "table"
            schema_root.mkdir(parents=True)
            self._write_definition(schema_root)
            users = FileMappedDatabase(root / "dat")["users"]
            self.assertEqual(users.schema.eng_name, "users")
            users.append({"user_id": 1, "name": "Alice", "status": "启用"})

    def test_invalid_definition_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.json").write_text(
                json.dumps({
                    "head": {"chn_name": "坏表", "eng_name": "bad"},
                    "columns": [{"chn_name": "编号", "eng_name": "id", "primary_key": False, "data_type": "int", "disp_type": "数字", "disp_menu": ""}],
                }),
                encoding="utf-8",
            )
            with self.assertRaises(SchemaError):
                TableDefinition.load(root, "bad")

    def test_old_field_names_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "old.json").write_text(
                json.dumps({
                    "head": {"cn_name": "旧表", "en_name": "old"},
                    "columns": [],
                }),
                encoding="utf-8",
            )
            with self.assertRaises(SchemaError):
                TableDefinition.load(root, "old")


if __name__ == "__main__":
    unittest.main()

