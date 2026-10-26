import json
import tempfile
import unittest
from pathlib import Path

from core import FileMappedDatabase, SchemaError, SchemaRegistry, TableDefinition, load_table_definition


class TableSchemaTests(unittest.TestCase):
    def _write_definition(self, root: Path) -> None:
        payload = {
            "head": {"chn_name": "用户表", "eng_name": "users", "max_elements": 3},
            "columns": [
                {"chn_name": "用户编号", "eng_name": "user_id", "primary_key": True, "data_type": "int", "data_length": 8, "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "用户名", "eng_name": "name", "primary_key": False, "data_type": "str", "data_length": 64, "disp_type": "文本", "disp_menu": ""},
                {"chn_name": "状态", "eng_name": "status", "primary_key": False, "data_type": "str", "data_length": 16, "disp_type": "下拉框", "disp_menu": "启用|停用"},
            ],
        }
        (root / "users.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_load_and_round_trip_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_definition(root)
            definition = load_table_definition(root, "users")
            self.assertEqual(definition.eng_name, "users")
            self.assertEqual(definition.max_elements, 3)
            self.assertEqual(definition.primary_keys, ("user_id",))
            self.assertEqual(definition.column("status").data_type, "str")
            self.assertEqual(definition.column("status").data_length, 16)
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
            with self.assertRaises(SchemaError):
                users.append({"user_id": 1, "name": "Duplicate", "status": "停用"})
            with self.assertRaises(SchemaError):
                users.append({"user_id": "wrong-type", "name": "Bad", "status": "停用"})
            users.extend([
                {"user_id": 2, "name": "Bob", "status": "启用"},
                {"user_id": 3, "name": "Cara", "status": "停用"},
            ])
            with self.assertRaises(SchemaError):
                users.append({"user_id": 4, "name": "Dan", "status": "启用"})

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
                    "head": {"chn_name": "坏表", "eng_name": "bad", "max_elements": 2},
                    "columns": [{"chn_name": "编号", "eng_name": "id", "primary_key": False, "data_type": "int", "data_length": 8, "disp_type": "数字", "disp_menu": ""}],
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
                    "head": {"cn_name": "旧表", "en_name": "old", "max_elements": 1},
                    "columns": [],
                }),
                encoding="utf-8",
            )
            with self.assertRaises(SchemaError):
                TableDefinition.load(root, "old")


if __name__ == "__main__":
    unittest.main()

