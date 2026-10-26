import multiprocessing as mp
import tempfile
import threading
import unittest

from core import FileMappedDatabase


def _worker(args):
    root, start, count = args
    table = FileMappedDatabase(root)["events"]
    for index in range(start, start + count):
        table.append({"id": index, "kind": "x" if index % 2 else "y"})


class FileMappedDatabaseTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

