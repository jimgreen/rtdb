import multiprocessing as mp
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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


def _hot_sync_update_worker(root, connection):
    """Update one fixed table while the parent keeps its table object open."""

    try:
        # The database and table are intentionally created only once in this
        # worker.  The parent must observe the update through its own already
        # opened mmap, row cache and secondary equality index.
        table = FileMappedDatabase(root)["users"]
        connection.send(("ready", None))
        command = connection.recv()
        if command != "update":
            raise RuntimeError(f"unexpected hot-sync command: {command!r}")
        changed = table.update(
            {"name": "Alicia", "group": "new"},
            {"id": 1},
        )
        connection.send(("updated", changed))
    except BaseException as exc:  # propagate the child failure to the test
        try:
            connection.send(("error", f"{type(exc).__name__}: {exc}"))
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise
    finally:
        connection.close()


class FileMappedDatabaseTests(unittest.TestCase):
    @staticmethod
    def _users_definition(root):
        return TableDefinition.from_mapping(
            {
                "head": {"chn_name": "用户", "eng_name": "users"},
                "columns": [
                    {"chn_name": "编号", "eng_name": "id", "primary_key": True, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                    {"chn_name": "姓名", "eng_name": "name", "primary_key": False, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
                    {"chn_name": "分组", "eng_name": "group", "primary_key": False, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
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

    def test_hot_sync_reuses_open_table_after_external_process_update(self):
        """An open fixed-table object sees another process's committed update."""

        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(
                Path(root) / "users.table",
                name="users",
                definition=definition,
            )
            table.insert_one({"id": 1, "name": "Alice", "group": "old"})

            # Prime both caches that historically could return stale data:
            # the primary-key row cache and the secondary equality index.
            self.assertEqual(table.kv[1], {"id": 1, "name": "Alice", "group": "old"})
            self.assertEqual(table.select({"group": "old"}), [
                {"id": 1, "name": "Alice", "group": "old"},
            ])
            self.assertEqual(
                table.select({"id": 1}, columns=["id", "name"]),
                [{"id": 1, "name": "Alice"}],
            )
            self.assertEqual(table.all(), [
                {"id": 1, "name": "Alice", "group": "old"},
            ])

            ctx = mp.get_context("spawn")
            parent, child = ctx.Pipe()
            process = ctx.Process(target=_hot_sync_update_worker, args=(root, child))
            process.start()
            child.close()
            try:
                self.assertTrue(parent.poll(10), "hot-sync worker did not start")
                self.assertEqual(parent.recv(), ("ready", None))
                parent.send("update")
                self.assertTrue(parent.poll(10), "hot-sync worker did not commit")
                status = parent.recv()
                self.assertEqual(status, ("updated", 1), status)
                process.join(10)
                self.assertFalse(process.is_alive(), "hot-sync worker did not exit")
                self.assertEqual(process.exitcode, 0)

                # Keep using the original ``table`` object.  No Database/Table
                # reconstruction or explicit refresh is allowed here.
                self.assertEqual(
                    table.kv[1],
                    {"id": 1, "name": "Alicia", "group": "new"},
                )
                self.assertEqual(
                    table.kv.get(1, fields=["id", "name"]),
                    {"id": 1, "name": "Alicia"},
                )
                self.assertEqual(table.select({"group": "old"}), [])
                self.assertEqual(
                    table.select({"group": "new"}),
                    [{"id": 1, "name": "Alicia", "group": "new"}],
                )
                self.assertEqual(table.all(), [
                    {"id": 1, "name": "Alicia", "group": "new"},
                ])
            finally:
                if process.is_alive():
                    process.terminate()
                process.join(10)
                parent.close()

    def test_delete_does_not_preserve_stale_secondary_index(self):
        """A later delete must not revive an equality index from an old generation."""

        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(
                Path(root) / "users.table",
                name="users",
                definition=definition,
            )
            table.insert_one({"id": 1, "name": "Alice", "group": "old"})

            # Prime both the primary and secondary caches on the long-lived
            # object, then commit a key-column update through another object.
            self.assertEqual(table.kv[1]["group"], "old")
            self.assertEqual(len(table.select({"group": "old"})), 1)
            other = Table(
                Path(root) / "users.table",
                name="users",
                definition=definition,
            )
            self.assertEqual(other.update({"group": "new"}, {"id": 1}), 1)

            # The delete preserves unchanged caches where safe, but the old
            # secondary cache belongs to the previous generation and must be
            # dropped instead of being incrementally edited with stale data.
            self.assertEqual(table.delete({"id": 1}), 1)
            self.assertEqual(table.select({"group": "old"}), [])
            self.assertEqual(table.select({"group": "new"}), [])
            other._close()

    def test_fixed_secondary_window_uses_index_order(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(
                Path(root) / "users.table",
                name="users",
                definition=definition,
            )
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": "same"}
                for index in range(1, 6)
            )
            self.assertEqual(
                table.select(
                    {"group": "same"},
                    columns=["id"],
                    offset=2,
                    limit=2,
                ),
                [{"id": 3}, {"id": 4}],
            )
            self.assertEqual(
                table.select(
                    {"group": "same"},
                    columns=["id"],
                    order_by="id DESC",
                    limit=2,
                ),
                [{"id": 5}, {"id": 4}],
            )

    def test_fixed_index_build_still_rejects_corrupt_payload(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(
                Path(root) / "users.table",
                name="users",
                definition=definition,
            )
            table.insert_one({"id": 1, "name": "Alice", "group": "a"})
            store = table._fixed_store
            assert store is not None
            with store._mapped() as (_handle, mapping):
                payload_offset = file_mapped_db._FIXED_DATA_OFFSET + file_mapped_db._FIXED_SLOT_HEADER.size
                mapping[payload_offset] ^= 0x01
                mapping.flush()
            with self.assertRaises(file_mapped_db.StorageError):
                table.kv.get(1)

    def test_persisted_fixed_index_is_reused_after_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            path = Path(root) / "users.table"
            table = Table(path, name="users", definition=definition)
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": "even" if index % 2 == 0 else "odd"}
                for index in range(100)
            )
            # Prime both the primary and secondary persisted indexes.
            self.assertEqual(table.kv[37]["name"], "User 37")
            self.assertEqual(len(table.select({"group": "odd"})), 50)
            index_paths = sorted(path.parent.glob(path.name + ".fixedidx.*.bin"))
            self.assertGreaterEqual(len(index_paths), 2)
            table._close()

            reopened = Table(path, name="users", definition=definition)
            with patch.object(reopened._fixed_store, "_build_key_index_from_map", side_effect=AssertionError("index was rebuilt")):
                self.assertEqual(reopened.kv[37]["name"], "User 37")
                self.assertEqual(len(reopened.select({"group": "odd"})), 50)
            reopened._close()

    def test_primary_probe_sidecar_serves_cold_point_lookup(self):
        """The fixed primary sidecar avoids decoding the full equality map."""

        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            path = Path(root) / "users.table"
            table = Table(path, name="users", definition=definition)
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": "same"}
                for index in range(100)
            )
            primary_paths = sorted(path.parent.glob(path.name + ".fixedpk.*.bin"))
            self.assertEqual(len(primary_paths), 1)
            self.assertGreaterEqual(
                primary_paths[0].stat().st_size,
                file_mapped_db._FIXED_PRIMARY_HEADER_SLOT * 2,
            )
            table._close()

            reopened = Table(path, name="users", definition=definition)
            with patch.object(
                reopened._fixed_store,
                "_decode_persisted_index",
                side_effect=AssertionError("cold primary lookup decoded the full map"),
            ):
                self.assertEqual(reopened.kv[37]["name"], "User 37")
            reopened._close()

    def test_corrupt_primary_probe_sidecar_falls_back_to_authoritative_index(self):
        """A damaged probe sidecar never changes the table result."""

        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            path = Path(root) / "users.table"
            table = Table(path, name="users", definition=definition)
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": "same"}
                for index in range(20)
            )
            table._close()
            primary_path = next(path.parent.glob(path.name + ".fixedpk.*.bin"))
            with primary_path.open("r+b") as handle:
                handle.seek(file_mapped_db._FIXED_PRIMARY_HEADER_SLOT * 2 + 1)
                original = handle.read(1)
                handle.seek(file_mapped_db._FIXED_PRIMARY_HEADER_SLOT * 2 + 1)
                handle.write(bytes([original[0] ^ 0x01]))
            reopened = Table(path, name="users", definition=definition)
            self.assertEqual(reopened.kv[7]["name"], "User 7")
            reopened._close()

    def test_corrupt_persisted_index_falls_back_to_safe_rebuild(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            path = Path(root) / "users.table"
            table = Table(path, name="users", definition=definition)
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": "same"}
                for index in range(20)
            )
            self.assertEqual(table.kv[7]["id"], 7)
            table._close()
            index_paths = sorted(path.parent.glob(path.name + ".fixedidx.*.bin"))
            self.assertTrue(index_paths)
            with index_paths[0].open("r+b") as handle:
                handle.seek(file_mapped_db._FIXED_INDEX_HEADER_SLOT * 2 + 3)
                original = handle.read(1)
                handle.seek(file_mapped_db._FIXED_INDEX_HEADER_SLOT * 2 + 3)
                handle.write(bytes([original[0] ^ 0x01]))
            reopened = Table(path, name="users", definition=definition)
            # A damaged side file is disposable; the authoritative table still
            # answers the query and repairs the index for the next open.
            self.assertEqual(reopened.kv[7]["id"], 7)
            self.assertEqual(len(reopened.select({"group": "same"})), 20)
            reopened._close()

    def test_non_index_update_advances_persisted_index_generation(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            path = Path(root) / "users.table"
            table = Table(path, name="users", definition=definition)
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": "same"}
                for index in range(10)
            )
            self.assertEqual(table.kv[3]["id"], 3)
            self.assertEqual(len(table.select({"group": "same"})), 10)
            index_paths = sorted(path.parent.glob(path.name + ".fixedidx.*.bin"))
            before = {item.name: item.read_bytes() for item in index_paths}
            self.assertEqual(table.update({"name": "Changed"}, {"id": 3}), 1)
            table._close()
            reopened = Table(path, name="users", definition=definition)
            with patch.object(reopened._fixed_store, "_build_key_index_from_map", side_effect=AssertionError("index was rebuilt")):
                self.assertEqual(reopened.kv[3]["name"], "Changed")
                self.assertEqual(len(reopened.select({"group": "same"})), 10)
            # The primary and secondary index files remain structurally stable;
            # only their redundant header generation changes.
            after = {item.name: item.read_bytes() for item in index_paths}
            self.assertEqual(set(before), set(after))
            for name in before:
                self.assertEqual(before[name][file_mapped_db._FIXED_INDEX_HEADER_SLOT * 2 :], after[name][file_mapped_db._FIXED_INDEX_HEADER_SLOT * 2 :])
            reopened._close()

    def test_value_update_leaves_unused_side_index_disposable_and_safe(self):
        """Unused equality sidecars may lag without affecting table results."""

        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            path = Path(root) / "users.table"
            table = Table(path, name="users", definition=definition)
            table.insert_many(
                [
                    {"id": 1, "name": "Alice", "group": "a"},
                    {"id": 2, "name": "Bob", "group": "a"},
                ]
            )
            store = table._fixed_store
            assert store is not None
            header = store._read_header()
            with store._mapped() as (_handle, mapping):
                name_index = store._build_key_index_from_map(mapping, ("name",))
            # Publish an index that this process has not loaded into its
            # equality cache.  A value-only update can then leave it at the
            # previous generation and let the next reader rebuild it safely.
            store._persist_key_index(("name",), header["generation"], name_index)
            name_path = store._index_path(("name",))
            before = name_path.read_bytes()
            before_header = max(
                filter(
                    None,
                    [
                        store._decode_index_header_slot(before[: file_mapped_db._FIXED_INDEX_HEADER_SLOT]),
                        store._decode_index_header_slot(
                            before[file_mapped_db._FIXED_INDEX_HEADER_SLOT : file_mapped_db._FIXED_INDEX_HEADER_SLOT * 2]
                        ),
                    ],
                ),
                key=lambda item: item["generation"],
            )
            self.assertEqual(table.update({"group": "b"}, {"id": 1}), 1)
            after = name_path.read_bytes()
            after_headers = [
                store._decode_index_header_slot(after[: file_mapped_db._FIXED_INDEX_HEADER_SLOT]),
                store._decode_index_header_slot(
                    after[file_mapped_db._FIXED_INDEX_HEADER_SLOT : file_mapped_db._FIXED_INDEX_HEADER_SLOT * 2]
                ),
            ]
            self.assertEqual(
                max(filter(None, after_headers), key=lambda item: item["generation"])["generation"],
                before_header["generation"],
            )
            table._close()

            # The stale optional sidecar is rejected by generation checking;
            # the authoritative table still returns the updated row.
            reopened = Table(path, name="users", definition=definition)
            self.assertEqual(reopened.select({"name": "Alice"}), [{"id": 1, "name": "Alice", "group": "b"}])
            reopened._close()

    def test_recreated_table_does_not_reuse_old_side_index(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            path = Path(root) / "users.table"
            table = Table(path, name="users", definition=definition)
            table.insert_many(
                [
                    {"id": 1, "name": "old", "group": "old"},
                    {"id": 2, "name": "old2", "group": "old"},
                ]
            )
            self.assertEqual(table.kv[1]["name"], "old")
            table._close()
            path.unlink()

            recreated = Table(path, name="users", definition=definition)
            with self.assertRaises(KeyError):
                recreated.kv[1]
            self.assertEqual(
                recreated._fixed_store._load_persisted_key_index(
                    ("id",), recreated._fixed_store._read_header()["generation"]
                ),
                {},
            )
            recreated._close()

    def test_structural_index_changes_append_delta_frames(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            path = Path(root) / "users.table"
            table = Table(path, name="users", definition=definition)
            table.insert_many(
                [
                    {"id": 1, "name": "one", "group": "a"},
                    {"id": 2, "name": "two", "group": "a"},
                ]
            )
            self.assertEqual(table.kv[1]["id"], 1)
            index_path = table._fixed_store._index_path(("id",))
            before = index_path.read_bytes()
            before_header = table._fixed_store._decode_index_header_slot(before[:128])
            before_payload_start = 256 + before_header["metadata_length"]
            before_payload = before[before_payload_start : before_payload_start + before_header["payload_length"]]

            table.append({"id": 3, "name": "three", "group": "a"})
            after = index_path.read_bytes()
            after_header = max(
                filter(
                    None,
                    [
                        table._fixed_store._decode_index_header_slot(after[:128]),
                        table._fixed_store._decode_index_header_slot(after[128:256]),
                    ],
                ),
                key=lambda item: item["generation"],
            )
            after_payload = after[before_payload_start : before_payload_start + after_header["payload_length"]]
            self.assertGreater(len(after_payload), len(before_payload))
            self.assertEqual(after_payload[: len(before_payload)], before_payload)
            table._close()

            reopened = Table(path, name="users", definition=definition)
            with patch.object(reopened._fixed_store, "_build_key_index_from_map", side_effect=AssertionError("index was rebuilt")):
                self.assertEqual(reopened.kv[3]["name"], "three")
                self.assertEqual(reopened.delete({"id": 1}), 1)
                self.assertEqual(reopened.kv.get(1), None)
                self.assertEqual(reopened.kv[2]["name"], "two")
            reopened._close()

    def test_other_process_append_updates_unloaded_secondary_side_index(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            path = Path(root) / "users.table"
            first = Table(path, name="users", definition=definition)
            first.insert_many(
                [
                    {"id": 1, "name": "one", "group": "a"},
                    {"id": 2, "name": "two", "group": "a"},
                ]
            )
            self.assertEqual(len(first.select({"group": "a"})), 2)
            first._close()

            # The writer only needs the primary index to append; the secondary
            # index is deliberately absent from its in-process cache.
            writer = Table(path, name="users", definition=definition)
            writer.append({"id": 3, "name": "three", "group": "a"})
            writer._close()

            reopened = Table(path, name="users", definition=definition)
            # A singleton append may intentionally leave the disposable
            # primary probe one generation behind; the authoritative equality
            # index still resolves the new row without reopening the table.
            self.assertEqual(reopened.kv[3]["name"], "three")
            with patch.object(reopened._fixed_store, "_build_key_index_from_map", side_effect=AssertionError("secondary index was rebuilt")):
                self.assertEqual(len(reopened.select({"group": "a"})), 3)
            reopened._close()

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
            self.assertEqual(table.path.read_bytes()[: len(file_mapped_db._FIXED_MAGIC)], file_mapped_db._FIXED_MAGIC)
            self.assertNotIn(b'"rows"', table.path.read_bytes())
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
            self.assertFalse(table.wal_path.exists())
            self.assertEqual(view.update_many({1: {"group": "c"}, 2: {"group": "d"}}), 2)
            self.assertEqual(view.set(2, {"name": "Robert", "group": "e"}), 1)
            self.assertEqual(view.set(3, {"name": "Cara", "group": "f"}, upsert=True), 1)
            self.assertEqual(view.get_many([3, 1, 404]), {3: {"id": 3, "name": "Cara", "group": "f"}, 1: {"id": 1, "name": "Alicia", "group": "c"}})
            self.assertEqual(view.delete(1), 1)
            reopened = FileMappedDatabase(root)["users"].key_value()
            self.assertEqual(reopened.keys(), [2, 3])
            self.assertEqual(reopened[2]["name"], "Robert")
            self.assertNotIn(1, reopened)

    def test_fixed_wal_recovers_after_interrupted_slot_publish(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_one({"id": 1, "name": "Alice", "group": "a"})
            store = table._fixed_store
            assert store is not None
            with file_mapped_db._locked(store.lock_path):
                header = store._read_header()
                with store._mapped() as (_handle, mapping):
                    slot = store._free_slot(mapping, header["row_count"])
                row = {"id": 2, "name": "Bob", "group": "b"}
                record = store._encode_record(
                    row,
                    store._order_for_generation(header["generation"] + 1),
                )
                # Simulate a process stopping after the durable WAL write but
                # before the slot and generation header are published.
                store._append_wal_locked(
                    header["generation"] + 1,
                    header["row_count"] + 1,
                    [(slot, record)],
                )
            reopened = Table(Path(root) / "users.table", name="users", definition=definition)
            self.assertEqual([row["id"] for row in reopened.all()], [1, 2])
            self.assertFalse(store.wal_path.exists() and store.wal_path.stat().st_size)

    def test_fixed_wal_stale_frame_after_durable_header_is_safe(self):
        """An unsynced post-commit WAL truncate must not affect recovery."""

        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_one({"id": 1, "name": "Alice", "group": "a"})
            store = table._fixed_store
            assert store is not None
            with file_mapped_db._locked(store.lock_path):
                header = store._read_header()
                with store._mapped() as (_handle, mapping):
                    order, row = store._read_record(mapping, 0)
                    assert row is not None
                    record = store._encode_record(row, order)
                # Model a crash after the durable table header was published
                # but before obsolete WAL truncation reached stable storage.
                # Recovery must ignore this already-covered generation.
                store._append_wal_locked(
                    header["generation"],
                    header["row_count"],
                    [(0, record)],
                )
            reopened = Table(Path(root) / "users.table", name="users", definition=definition)
            self.assertEqual(reopened.all(), [{"id": 1, "name": "Alice", "group": "a"}])
            # Append-only WAL frames are retained until the checkpoint bound
            # is reached.  A stale frame already covered by the durable table
            # generation is safe to keep and must not be replayed twice.
            self.assertGreater(store.wal_path.stat().st_size, 0)

    def test_fixed_batch_wal_replays_all_slots_after_interruption(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_many([
                {"id": 1, "name": "Alice", "group": "a"},
                {"id": 2, "name": "Bob", "group": "b"},
            ])
            store = table._fixed_store
            assert store is not None
            with file_mapped_db._locked(store.lock_path):
                header = store._read_header()
                entries = []
                with store._mapped() as (_handle, mapping):
                    for slot, row in ((0, {"id": 1, "name": "A", "group": "a"}), (1, {"id": 2, "name": "B", "group": "b"})):
                        order, _current = store._read_record(mapping, slot)
                        entries.append((slot, store._encode_record(row, order)))
                # A process can stop after WAL fsync and before any slot or
                # header is published.  Recovery must replay the complete
                # batch atomically, rather than exposing only one row.
                store._append_wal_locked(header["generation"] + 1, header["row_count"], entries)
            reopened = Table(Path(root) / "users.table", name="users", definition=definition)
            self.assertEqual([row["name"] for row in reopened.all()], ["A", "B"])
            self.assertFalse(store.wal_path.exists() and store.wal_path.stat().st_size)

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
                    "head": {"chn_name": "关系", "eng_name": "links"},
                    "columns": [
                        {"chn_name": "站点", "eng_name": "site", "primary_key": True, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                        {"chn_name": "设备", "eng_name": "device", "primary_key": True, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                        {"chn_name": "状态", "eng_name": "state", "primary_key": False, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
                    ],
                },
                root,
                "links",
            )
            table = Table(Path(root) / "links.table", name="links", definition=definition)
            table.insert_many([
                {"site": 1, "device": 2, "state": "up"},
                {"site": 1, "device": 3, "state": "up"},
            ])
            view = table.kv
            self.assertEqual(view[(1, 2)]["state"], "up")
            self.assertEqual(view.update((1, 2), {"state": "down"}), 1)
            self.assertEqual(view[(1, 2)]["state"], "down")
            self.assertEqual(table.get_by_pk([1, 2])["state"], "down")
            self.assertEqual(table.select({"site": 1, "device": 2}), [{"site": 1, "device": 2, "state": "down"}])
            self.assertEqual(
                view.get_many([[1, 2], (1, 3)]),
                {
                    (1, 2): {"site": 1, "device": 2, "state": "down"},
                    (1, 3): {"site": 1, "device": 3, "state": "up"},
                },
            )
            self.assertEqual(view.update_many({(1, 2): {"state": "ready"}, (1, 3): {"state": "standby"}}), 2)
            with self.assertRaises(file_mapped_db.SchemaError):
                table.insert_one({"site": 1, "device": 2, "state": "duplicate"})
            self.assertEqual(table.delete_by_pk([1, 3]), 1)
            reopened = FileMappedDatabase(root)["links"]
            self.assertEqual(reopened.kv[(1, 2)]["state"], "ready")
            with self.assertRaises(KeyError):
                _ = reopened.kv[(2, 1)]

    def test_fixed_primary_key_update_refreshes_index_and_rejects_duplicates(self):
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
            # Build a secondary equality index before exercising the primary
            # key update path.  A primary-key addressed patch that changes a
            # secondary field must invalidate that cached bucket atomically.
            self.assertEqual(table.select({"group": "a"})[0]["id"], 1)
            self.assertEqual(table.update({"group": "b"}, {"id": 1}), 1)
            self.assertEqual(table.select({"group": "a"}), [])
            self.assertEqual([row["id"] for row in table.select({"group": "b"})], [1, 2])
            self.assertEqual(table.update({"group": "a"}, {"id": 1}), 1)
            self.assertEqual(table.update({"id": 3}, {"id": 1}), 1)
            self.assertEqual(table.kv[3]["name"], "Alice")
            with self.assertRaises(file_mapped_db.SchemaError):
                table.update({"id": 2}, {"id": 3})

    def test_fixed_equality_crud_uses_indexed_slots_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as root:
            table = Table(
                Path(root) / "users.table",
                name="users",
                definition=self._users_definition(root),
            )
            table.insert_many([
                {"id": 1, "name": "Alice", "group": "a"},
                {"id": 2, "name": "Bob", "group": "b"},
                {"id": 3, "name": "Cara", "group": "a"},
                {"id": 4, "name": "Dan", "group": "a"},
            ])
            self.assertEqual(
                table.execute("UPDATE users SET name = 'Alicia' WHERE group = 'a' LIMIT 1").rowcount,
                1,
            )

            # The non-primary equality path decodes only the requested
            # projection and honours persisted insertion order before ORDER BY.
            self.assertEqual(
                table.select(
                    {"group": "a"},
                    columns=["id", "name"],
                    order_by="id DESC",
                    limit=2,
                    offset=0,
                ),
                [{"id": 4, "name": "Dan"}, {"id": 3, "name": "Cara"}],
            )
            self.assertEqual(
                table.select({"group": "a"})[0],
                {"id": 1, "name": "Alicia", "group": "a"},
            )
            self.assertEqual(
                table.select({"group": "a"}, columns=["id"], limit=1, offset=1),
                [{"id": 3}],
            )

            # A local slot update changes only the first matching row when a
            # limit is supplied, and a primary-key collision is rejected
            # atomically before any slot is published.
            self.assertEqual(table.update({"name": "updated"}, {"group": "a"}, limit=1), 1)
            self.assertEqual(table.kv[1]["name"], "updated")
            self.assertEqual(table.kv[3]["name"], "Cara")
            self.assertEqual(table.update({"group": "c"}, {"group": "a"}, limit=1), 1)
            self.assertEqual([row["id"] for row in table.select({"group": "a"})], [3, 4])
            self.assertEqual([row["id"] for row in table.select({"group": "c"})], [1])
            with self.assertRaises(file_mapped_db.SchemaError):
                table.update({"id": 2}, {"group": "a"}, limit=1)
            self.assertEqual(table.kv[1]["id"], 1)
            self.assertEqual(table.kv[2]["group"], "b")

            self.assertEqual(table.delete({"group": "a"}, limit=1), 1)
            self.assertEqual([row["id"] for row in table.all()], [1, 2, 4])
            self.assertEqual(table.delete({"group": "a"}), 1)
            self.assertEqual(table.delete({"group": "c"}), 1)
            self.assertEqual(table.all(), [{"id": 2, "name": "Bob", "group": "b"}])

            reopened = Table(
                Path(root) / "users.table",
                name="users",
                definition=self._users_definition(root),
            )
            self.assertEqual(reopened.all(), [{"id": 2, "name": "Bob", "group": "b"}])

    def test_fixed_secondary_indexes_survive_insert_and_delete(self):
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
            store = table._fixed_store
            assert store is not None
            self.assertEqual(table.select({"group": "a"})[0]["id"], 1)
            with patch.object(store, "_build_key_index_from_map", wraps=store._build_key_index_from_map) as build:
                table.insert_one({"id": 3, "name": "Cara", "group": "a"})
                self.assertEqual([row["id"] for row in table.select({"group": "a"})], [1, 3])
                table.delete({"group": "a"}, limit=1)
                self.assertEqual([row["id"] for row in table.select({"group": "a"})], [3])
                self.assertEqual(build.call_count, 0)

    def test_fixed_batch_equality_updates_commit_once_and_are_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            table = Table(
                Path(root) / "users.table",
                name="users",
                definition=self._users_definition(root),
            )
            table.insert_many([
                {"id": 1, "name": "Alice", "group": "a"},
                {"id": 2, "name": "Bob", "group": "b"},
                {"id": 3, "name": "Cara", "group": "a"},
            ])
            self.assertEqual(
                table.update_many_where([
                    ({"group": "a"}, {"name": "A"}),
                    ({"group": "b"}, {"name": "B"}),
                ]),
                3,
            )
            self.assertEqual([row["name"] for row in table.all()], ["A", "B", "A"])
            with self.assertRaises(file_mapped_db.SchemaError):
                table.update_many_where([
                    ({"group": "a"}, {"id": 2}),
                    ({"group": "b"}, {"name": "should-not-publish"}),
                ])
            self.assertEqual(table.kv[1]["name"], "A")
            self.assertEqual(table.kv[2]["name"], "B")
            self.assertEqual(table.delete_many_where([{"group": "a"}, {"group": "b"}]), 3)
            self.assertEqual(table.all(), [])

    def test_write_batch_uses_one_durable_commit_for_many_primary_updates(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(
                Path(root) / "users.table",
                name="users",
                definition=definition,
            )
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": "same"}
                for index in range(12)
            )
            # Prime the primary side index before measuring the explicit
            # group-commit path.  The side index is an optional cache and does
            # not participate in the durability count.
            self.assertEqual(table.kv[3]["id"], 3)
            real_fsync = file_mapped_db.os.fsync
            with patch.object(file_mapped_db.os, "fsync", wraps=real_fsync) as synced:
                with table.transaction() as batch:
                    for index in range(12):
                        batch.update_by_pk(index, {"name": f"Changed {index}"})
            # The WAL frame is the per-commit durability boundary.  Table
            # pages are flushed by the bounded checkpoint, so this hot batch
            # needs one durable sync (the WAL) while retaining the same CRC
            # and generation recovery guarantees.
            self.assertEqual(synced.call_count, 1)
            self.assertEqual(table.kv[7]["name"], "Changed 7")

    def test_fixed_wal_checkpoint_flushes_and_truncates(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            old_threshold = file_mapped_db._FIXED_WAL_CHECKPOINT_BYTES
            file_mapped_db._FIXED_WAL_CHECKPOINT_BYTES = 1
            try:
                table.insert_one({"id": 1, "name": "Alice", "group": "a"})
                store = table._fixed_store
                assert store is not None
                self.assertEqual(store.wal_path.stat().st_size, 0)
                self.assertEqual(table.kv[1]["name"], "Alice")
                reopened = Table(Path(root) / "users.table", name="users", definition=definition)
                self.assertEqual(reopened.kv[1]["name"], "Alice")
            finally:
                file_mapped_db._FIXED_WAL_CHECKPOINT_BYTES = old_threshold

    def test_fixed_wal_crc_error_in_pending_tail_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            store = table._fixed_store
            assert store is not None
            with file_mapped_db._locked(store.lock_path):
                header = store._read_header()
                with store._mapped() as (_handle, mapping):
                    slot = store._free_slot(mapping, header["row_count"])
                record = store._encode_record(
                    {"id": 99, "name": "pending", "group": "crc"},
                    store._order_for_generation(header["generation"] + 1),
                )
                store._append_wal_locked(
                    header["generation"] + 1,
                    header["row_count"] + 1,
                    [(slot, record)],
                )
            payload = bytearray(store.wal_path.read_bytes())
            payload[-20] ^= 0x01
            store.wal_path.write_bytes(payload)
            with self.assertRaises(file_mapped_db.StorageError):
                Table(Path(root) / "users.table", name="users", definition=definition).all()

    def test_fixed_wal_repairs_header_ahead_of_record_page(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_one({"id": 1, "name": "Alice", "group": "a"})
            store = table._fixed_store
            assert store is not None
            with file_mapped_db._locked(store.lock_path):
                header = store._read_header()
                with store._mapped() as (_handle, mapping):
                    order, _current = store._read_record(mapping, 0)
                repaired = store._encode_record(
                    {"id": 1, "name": "Replayed", "group": "a"},
                    order,
                )
                # Model an interrupted publish in which the generation header
                # reached stable storage but the changed record page did not.
                store._append_wal_locked(header["generation"], header["row_count"], [(0, repaired)])
            reopened = Table(Path(root) / "users.table", name="users", definition=definition)
            self.assertEqual(reopened.kv[1]["name"], "Replayed")

    def test_write_batch_rejects_mixed_operation_families(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_one({"id": 1, "name": "Alice", "group": "a"})
            with self.assertRaises(ValueError):
                with table.transaction() as batch:
                    batch.update_by_pk(1, {"name": "Alicia"})
                    batch.delete_by_pk(1)
            self.assertEqual(table.kv[1]["name"], "Alice")

    def test_hot_row_lru_refreshes_local_updates_and_multi_gets(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": "same"}
                for index in range(4)
            )
            self.assertEqual(table.kv[1]["name"], "User 1")
            self.assertEqual(table.kv[2]["name"], "User 2")
            self.assertEqual(
                table.kv.get_many([1, 2]),
                {
                    1: {"id": 1, "name": "User 1", "group": "same"},
                    2: {"id": 2, "name": "User 2", "group": "same"},
                },
            )
            self.assertEqual(table.update_by_pk(1, {"name": "Changed"}), 1)
            # The locally published slot refreshes its cached value while an
            # unrelated hot row remains available through the same generation.
            self.assertEqual(table.kv[1]["name"], "Changed")
            self.assertEqual(table.kv.get_many([2])[2]["name"], "User 2")
            store = table._fixed_store
            assert store is not None
            self.assertLessEqual(len(store._row_cache), file_mapped_db._FIXED_ROW_CACHE_LIMIT)

    def test_read_pipeline_coalesces_commands_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": "same"}
                for index in range(1, 4)
            )
            pipe = table.kv.read_pipeline()
            self.assertIs(pipe.get(2), pipe)
            pipe.contains(99).get_many([3, 1]).get(2)
            result = pipe.execute()
            self.assertEqual(
                result,
                [
                    {"id": 2, "name": "User 2", "group": "same"},
                    False,
                    {
                        3: {"id": 3, "name": "User 3", "group": "same"},
                        1: {"id": 1, "name": "User 1", "group": "same"},
                    },
                    {"id": 2, "name": "User 2", "group": "same"},
                ],
            )
            self.assertEqual(len(pipe), 0)
            # Results are detached per command even when the same key appears
            # more than once in one pipeline.
            result[0]["name"] = "mutated"
            self.assertEqual(table.kv[2]["name"], "User 2")

    def test_process_local_read_write_lock_supports_upgrade_and_reentrancy(self):
        guard = file_mapped_db._ReadWriteLock()
        with guard.read():
            with guard:
                with guard.read():
                    pass
        with guard:
            with guard:
                pass

    def test_cold_index_construction_is_single_flight(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": f"group-{index % 3}"}
                for index in range(200)
            )
            store = table._fixed_store
            assert store is not None
            for path in Path(root).glob("users.table.fixedidx.*.bin"):
                path.unlink()
            with store._key_cache_guard:
                store._key_cache.clear()
            real_builder = store._build_key_index_from_map
            with patch.object(store, "_build_key_index_from_map", wraps=real_builder) as builder:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    rows = list(executor.map(lambda _item: table.kv.get(77), range(8)))
            self.assertEqual([row["id"] for row in rows], [77] * 8)
            self.assertEqual(builder.call_count, 1)

    def test_fixed_projection_cache_is_generation_checked_and_refreshed(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_one({"id": 1, "name": "Alice", "group": "a"})
            store = table._fixed_store
            assert store is not None

            self.assertEqual(
                table.select({"id": 1}, columns=["id", "name"]),
                [{"id": 1, "name": "Alice"}],
            )
            first = table.cache_stats()
            self.assertEqual(first["projection_cache_misses"], 1)
            self.assertEqual(first["projection_cache_hits"], 0)
            self.assertEqual(first["projection_cache_size"], 1)

            self.assertEqual(
                table.select({"id": 1}, columns=["id", "name"]),
                [{"id": 1, "name": "Alice"}],
            )
            self.assertEqual(
                table.kv.get(1, fields=["id", "name"]),
                {"id": 1, "name": "Alice"},
            )
            second = table.cache_stats()
            self.assertGreaterEqual(second["projection_cache_hits"], 1)

            table.update_by_pk(1, {"group": "b"})
            before_name_update = table.cache_stats()
            self.assertEqual(
                table.select({"id": 1}, columns=["id", "name"]),
                [{"id": 1, "name": "Alice"}],
            )
            after_unrelated_update = table.cache_stats()
            self.assertGreaterEqual(
                after_unrelated_update["projection_cache_hits"],
                before_name_update["projection_cache_hits"] + 1,
            )

            table.update_by_pk(1, {"name": "Alicia"})
            self.assertEqual(
                table.select({"id": 1}, columns=["id", "name"]),
                [{"id": 1, "name": "Alicia"}],
            )
            third = table.cache_stats()
            self.assertGreaterEqual(third["projection_cache_hits"], second["projection_cache_hits"] + 1)
            self.assertEqual(third["projection_cache_size"], 1)

    def test_primary_slot_cache_accelerates_hot_key_and_tracks_generation(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_many(
                {"id": index, "name": f"User {index}", "group": f"group-{index % 3}"}
                for index in range(20)
            )
            store = table._fixed_store
            assert store is not None
            # The first call resolves through the authoritative equality index;
            # subsequent calls use the direct primary-key -> slot map.
            self.assertEqual(table.kv.get(7)["name"], "User 7")
            first = table.cache_stats()
            self.assertEqual(first["primary_slot_cache_size"], 1)
            self.assertEqual(first["primary_slot_hits"], 0)
            with store._row_cache_guard:
                store._row_cache.clear()
            self.assertEqual(table.kv.get(7)["name"], "User 7")
            second = table.cache_stats()
            self.assertGreaterEqual(second["primary_slot_hits"], 1)

            before = table.cache_stats()["primary_slot_hits"]
            table.kv.update(7, {"name": "changed"})
            with store._row_cache_guard:
                store._row_cache.clear()
            self.assertEqual(table.kv.get(7)["name"], "changed")
            after = table.cache_stats()
            # A value-only local commit advances the cached generation instead
            # of dropping the direct slot, so the post-write read remains hot.
            self.assertGreater(after["primary_slot_hits"], before)

    def test_local_publish_refreshes_header_cache_without_redecode(self):
        with tempfile.TemporaryDirectory() as root:
            definition = self._users_definition(root)
            table = Table(Path(root) / "users.table", name="users", definition=definition)
            table.insert_one({"id": 1, "name": "Alice", "group": "a"})
            store = table._fixed_store
            assert store is not None
            with patch.object(
                store,
                "_decode_header_slot",
                wraps=store._decode_header_slot,
            ) as decode_header:
                table.update_by_pk(1, {"name": "Alicia"})
            self.assertEqual(decode_header.call_count, 0)
            self.assertEqual(table.kv[1]["name"], "Alicia")

    def test_read_pipeline_supports_composite_primary_keys_and_stats(self):
        with tempfile.TemporaryDirectory() as root:
            definition = TableDefinition.from_mapping(
                {
                    "head": {"chn_name": "关联", "eng_name": "links"},
                    "columns": [
                        {"chn_name": "站点", "eng_name": "site", "primary_key": True, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
                        {"chn_name": "编号", "eng_name": "device", "primary_key": True, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                        {"chn_name": "状态", "eng_name": "state", "primary_key": False, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
                    ],
                },
                root,
                "links",
            )
            table = Table(Path(root) / "links.table", name="links", definition=definition)
            table.insert_many(
                [
                    {"site": "a", "device": 1, "state": "up"},
                    {"site": "b", "device": 2, "state": "down"},
                ]
            )
            result = table.read_pipeline().get(("b", 2)).contains(("x", 9)).execute()
            self.assertEqual(result[0]["state"], "down")
            self.assertFalse(result[1])
            self.assertEqual(table.read_pipeline().get(("b", 2)).execute()[0]["state"], "down")
            stats = table.cache_stats()
            self.assertGreaterEqual(stats["get_many_calls"], 1)
            self.assertGreaterEqual(stats["row_cache_hits"], 1)
            self.assertIn("index_hits", stats)


if __name__ == "__main__":
    unittest.main()

