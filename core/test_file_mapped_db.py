import multiprocessing as mp
import tempfile
import threading
import unittest
from pathlib import Path

import core.file_mapped_db as file_mapped_db
from core import FileMappedDatabase, Table, TableDefinition


def _worker(args):
    root, start, count = args
    table = FileMappedDatabase(root)["events"]
    for index in range(start, start + count):
        table.append({"id": index, "kind": "x" if index % 2 else "y"})


def _key_update_worker(args):
    root, start, count = args
    view = FileMappedDatabase(root)["events"].kv
    for index in range(start, start + count):
        view.update(index, {"value": index + 1})


class FileMappedDatabaseTests(unittest.TestCase):
    @staticmethod
    def _users_definition(root):
        return TableDefinition.from_mapping(
            {
                "head": {"chn_name": "用户", "eng_name": "users", "max_elements": 1000},
                "columns": [
                    {"chn_name": "编号", "eng_name": "id", "primary_key": True, "data_type": "int", "data_length": 8, "disp_type": "数字", "disp_menu": ""},
                    {"chn_name": "姓名", "eng_name": "name", "primary_key": False, "data_type": "str", "data_length": 32, "disp_type": "文本", "disp_menu": ""},
                    {"chn_name": "分组", "eng_name": "group", "primary_key": False, "data_type": "str", "data_length": 16, "disp_type": "文本", "disp_menu": ""},
                ],
            },
            root,
            "users",
        )

    def test_crud_sql_and_list_api(self):
        with tempfile.TemporaryDirectory() as root:
            db = FileMappedDatabase(root)
            users = db["users"]
            users.extend(
                [
                    {"id": 1, "name": "Alice", "age": 31},
                    {"id": 2, "name": "Bob", "age": 19},
                    {"id": 3, "name": "Cara", "age": 27},
                ]
            )
            self.assertEqual(
                db.execute("SELECT id, name FROM users WHERE age >= 20 ORDER BY age DESC").rows,
                [{"id": 1, "name": "Alice"}, {"id": 3, "name": "Cara"}],
            )
            self.assertEqual(
                db.execute("INSERT INTO users (id, name, age) VALUES (4, 'Dan', 40), (5, 'Eve', 22)").rowcount,
                2,
            )
            self.assertEqual(db.execute("UPDATE users SET age = 32 WHERE id = 1").rowcount, 1)
            self.assertEqual(db.execute("UPDATE users SET age = 33, name = 'Alicia' WHERE id = 1").rowcount, 1)
            self.assertEqual(db.execute("DELETE FROM users WHERE age < 20").rowcount, 1)
            self.assertEqual(users[0]["name"], "Alicia")
            users[0] = {"id": 1, "name": "Alicia", "age": 33}
            users.insert(0, {"id": 0, "name": "Root", "age": 1})
            self.assertEqual(users.pop(0)["id"], 0)
            self.assertEqual(users.count("age >= 20"), 4)
            self.assertEqual(users.select(order_by="age DESC", limit=1)[0]["age"], 40)

    def test_multi_process_appends_are_not_lost(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = mp.get_context("spawn")
            with ctx.Pool(4) as pool:
                pool.map(_worker, [(root, index * 25, 25) for index in range(4)])
            events = FileMappedDatabase(root)["events"]
            self.assertEqual(len(events), 100)
            self.assertEqual(events.count({"kind": "x"}), 50)
            self.assertEqual(events.select(order_by="id ASC", limit=2)[0]["id"], 0)

    def test_multi_process_key_updates_are_serialised(self):
        with tempfile.TemporaryDirectory() as root:
            table = FileMappedDatabase(root)["events"]
            table.insert_many({"id": index, "value": 0} for index in range(100))
            ctx = mp.get_context("spawn")
            with ctx.Pool(4) as pool:
                pool.map(_key_update_worker, [(root, index * 25, 25) for index in range(4)])
            events = FileMappedDatabase(root)["events"].kv
            self.assertEqual(len(events), 100)
            self.assertEqual(sum(events[key]["value"] for key in events), sum(range(1, 101)))

    def test_predicates_aliases_threads_and_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            db = FileMappedDatabase(root)
            users = db["users"]
            users.extend(
                [
                    {"id": 1, "name": "Alice", "age": 31},
                    {"id": 2, "name": "Bob", "age": None},
                    {"id": 3, "name": "Cara", "age": 27},
                ]
            )
            result = db.execute("SELECT name AS label FROM users WHERE name LIKE 'A%' OR age IS NULL ORDER BY name ASC")
            self.assertEqual(result.rows, [{"label": "Alice"}, {"label": "Bob"}])
            self.assertEqual(db.execute("UPDATE users SET age = 20 WHERE age >= 20 LIMIT 1").rowcount, 1)
            self.assertEqual(db.execute("DELETE FROM users WHERE id IN (2, 99)").rowcount, 1)

            def append_rows(start):
                for index in range(start, start + 20):
                    users.append({"id": index, "name": f"N{index}", "age": index})

            threads = [threading.Thread(target=append_rows, args=(100 * number,)) for number in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            reopened = FileMappedDatabase(root)["users"]
            self.assertEqual(len(reopened), 82)
            self.assertEqual(reopened[0]["name"], "Alice")
            self.assertEqual(reopened.select(where={"id": 319})[0]["name"], "N319")

    def test_wal_replays_after_reopen_and_repairs_partial_tail(self):
        with tempfile.TemporaryDirectory() as root:
            table = FileMappedDatabase(root)["events"]
            table.insert_many([{"id": 1, "value": "a"}, {"id": 2, "value": "b"}])
            with table.wal_path.open("ab") as handle:
                handle.write(b"RTDBWAL")
            reopened = FileMappedDatabase(root)["events"]
            self.assertEqual(reopened.all(), [{"id": 1, "value": "a"}, {"id": 2, "value": "b"}])
            self.assertEqual(reopened.update({"value": "updated"}, {"id": 1}), 1)
            self.assertEqual(reopened.delete({"id": 2}), 1)
            self.assertEqual(FileMappedDatabase(root)["events"].all(), [{"id": 1, "value": "updated"}])
            reopened.append({"id": 3, "value": "c"})
            self.assertEqual(len(FileMappedDatabase(root)["events"]), 2)

    def test_wal_checksum_corruption_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            table = FileMappedDatabase(root)["events"]
            table.append({"id": 1})
            with table.wal_path.open("r+b") as handle:
                handle.seek(file_mapped_db._WAL_HEADER.size)
                original = handle.read(1)
                handle.seek(file_mapped_db._WAL_HEADER.size)
                handle.write(bytes([original[0] ^ 0x01]))
            with self.assertRaises(file_mapped_db.StorageError):
                FileMappedDatabase(root)["events"].all()

    def test_wal_sequence_gap_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            table = FileMappedDatabase(root)["events"]
            table.append({"id": 1})
            frame = file_mapped_db._wal_frame(
                {"version": 1, "seq": 3, "op": "append", "rows": [{"id": 3}]}
            )
            with table.wal_path.open("ab") as handle:
                handle.write(frame)
                handle.flush()
            with self.assertRaises(file_mapped_db.StorageError):
                FileMappedDatabase(root)["events"].all()

    def test_wal_checkpoint_rotation_is_safe_with_an_old_base(self):
        old_threshold = file_mapped_db._WAL_COMPACT_BYTES
        file_mapped_db._WAL_COMPACT_BYTES = 1
        try:
            with tempfile.TemporaryDirectory() as root:
                table = FileMappedDatabase(root)["events"]
                old_base = table.path.read_bytes()
                table.insert_many([{"id": 1}, {"id": 2}])
                table.path.write_bytes(old_base)
                self.assertEqual(FileMappedDatabase(root)["events"].all(), [{"id": 1}, {"id": 2}])
        finally:
            file_mapped_db._WAL_COMPACT_BYTES = old_threshold

    def test_key_value_view_uses_index_and_replays_wal(self):
        with tempfile.TemporaryDirectory() as root:
            table = Table(
                Path(root) / "users.table",
                name="users",
                definition=self._users_definition(root),
            )
            table.insert_many([
                {"id": 1, "name": "Alice", "group": "a"},
                {"id": 2, "name": "Bob", "group": "b"},
            ])
            view = table.key_value()
            detached = view[1]
            detached["name"] = "mutated outside the table"
            self.assertEqual(view[1]["name"], "Alice")
            self.assertEqual(view.get(99, {"missing": True}), {"missing": True})
            self.assertTrue(view.contains(2))
            self.assertEqual(view.update(1, {"name": "Alicia"}), 1)
            self.assertEqual(view.update_many({1: {"group": "c"}, 2: {"group": "d"}}), 2)
            self.assertEqual(view.set(2, {"name": "Robert", "group": "e"}), 1)
            self.assertEqual(view.set(3, {"name": "Cara", "group": "f"}, upsert=True), 1)
            self.assertEqual(view.get_many([3, 1, 404]), {3: {"id": 3, "name": "Cara", "group": "f"}, 1: {"id": 1, "name": "Alicia", "group": "c"}})
            self.assertEqual(view.delete(1), 1)
            reopened = FileMappedDatabase(root)["users"].key_value()
            self.assertEqual(reopened.keys(), [2, 3])
            self.assertEqual(reopened[2]["name"], "Robert")
            self.assertNotIn(1, reopened)

    def test_key_value_explicit_column_and_atomic_upsert(self):
        with tempfile.TemporaryDirectory() as root:
            table = FileMappedDatabase(root)["events"]
            table.insert_many([
                {"id": 1, "token": "a", "value": 10},
                {"id": 2, "token": "b", "value": 20},
            ])
            view = table.key_value("token")
            self.assertEqual(view["b"]["id"], 2)
            self.assertEqual(view.update("a", {"value": 11}), 1)
            self.assertEqual(view.update("c", {"value": 30}, upsert=True), 1)
            self.assertEqual(view["c"], {"token": "c", "value": 30})
            self.assertEqual(view.delete_many(["a", "c"]), 2)
            self.assertEqual(view.keys(), ["b"])
            reopened = FileMappedDatabase(root)["events"].key_value("token")
            self.assertEqual(reopened.items(), [("b", {"id": 2, "token": "b", "value": 20})])

    def test_key_value_composite_primary_key(self):
        with tempfile.TemporaryDirectory() as root:
            definition = TableDefinition.from_mapping(
                {
                    "head": {"chn_name": "关系", "eng_name": "links", "max_elements": 100},
                    "columns": [
                        {"chn_name": "站点", "eng_name": "site", "primary_key": True, "data_type": "int", "data_length": 8, "disp_type": "数字", "disp_menu": ""},
                        {"chn_name": "设备", "eng_name": "device", "primary_key": True, "data_type": "int", "data_length": 8, "disp_type": "数字", "disp_menu": ""},
                        {"chn_name": "状态", "eng_name": "state", "primary_key": False, "data_type": "str", "data_length": 8, "disp_type": "文本", "disp_menu": ""},
                    ],
                },
                root,
                "links",
            )
            table = Table(Path(root) / "links.table", name="links", definition=definition)
            table.insert_many([{"site": 1, "device": 2, "state": "up"}])
            view = table.kv
            self.assertEqual(view[(1, 2)]["state"], "up")
            self.assertEqual(view.update((1, 2), {"state": "down"}), 1)
            self.assertEqual(view[(1, 2)]["state"], "down")
            with self.assertRaises(KeyError):
                _ = view[(2, 1)]


if __name__ == "__main__":
    unittest.main()

