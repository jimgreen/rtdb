# File-mapped table interface

`file_mapped_db.py` provides a dependency-free relation-style data store. Each
table is one `<name>.table` file under the database directory and is read using
`mmap`. A side-car `<name>.table.lock` file serializes complete operations
across processes; a process-local re-entrant lock additionally protects threads.
Writes use a temporary file, `fsync`, and `os.replace`, so readers never see a
partially written snapshot.

```python
from core import FileMappedDatabase

db = FileMappedDatabase("./data")
users = db.table("users")
users.extend([
    {"id": 1, "name": "Alice", "age": 31},
    {"id": 2, "name": "Bob", "age": 19},
])

adults = users.select({"age": 31})
recent = db.execute(
    "SELECT id, name FROM users WHERE age >= 18 "
    "ORDER BY age DESC LIMIT 20"
)
db.execute("UPDATE users SET age = 32 WHERE id = 1")
db.execute("DELETE FROM users WHERE id = 2")

# List-like access is persisted too.
users.append({"id": 3, "name": "Cara", "age": 27})
first = users[0]
users[0] = {"id": first["id"], "name": first["name"], "age": 33}
del users[-1]
```

Supported SQL is intentionally small and explicit:

* `SELECT ... FROM ... [WHERE ...] [ORDER BY ...] [LIMIT ...] [OFFSET ...]`
* `INSERT INTO ... (columns) VALUES (...), (...)`
* `UPDATE ... SET column = value [, ...] [WHERE ...] [LIMIT ...]`
* `DELETE FROM ... [WHERE ...] [LIMIT ...]`

`WHERE` supports `AND`, `OR`, comparisons, `LIKE`, `IN`, `IS NULL`, and
`IS NOT NULL`. Values must be JSON-compatible mappings; column and table names
use Python/SQL identifier syntax. `QueryResult` is a list with `rowcount` and
`lastrowid` attributes. Reads of an unchanged file use the in-process parsed
snapshot cache; for many writes, prefer `insert_many`/`extend` so one atomic
snapshot is written for the whole batch.

Optional JSON definitions can be placed in `def/table` next to the data
directory. `FileMappedDatabase` automatically discovers one `<table>.json`
definition file and exposes the parsed definition
as `table.schema`. The definition enforces maximum row count, declared column
names, non-null primary keys, and primary-key uniqueness during reads and
writes. See [def/table/README.md](../def/table/README.md) for the exact JSON
format.

