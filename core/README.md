# File-mapped table interface

`file_mapped_db.py` provides a dependency-free relation-style data store. Each
table is one `<name>.table` file under the database directory and is read using
`mmap`. A side-car `<name>.table.lock` file serializes write transactions
across processes; a process-local re-entrant lock additionally protects
writers in one process. Readers use an optimistic immutable-snapshot path, so
multiple threads can read concurrently while Windows writers retry an atomic
replace if a reader still has the old file open. Writes use a temporary file,
`fsync`, and `os.replace`, so readers never see a partially written snapshot.

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
snapshot cache. Equality queries use a primary-key or `id` index when
available. The explicit `get_by_pk`, `update_by_pk`, and `delete_by_pk`
methods avoid a scan for declared primary keys. For many writes, prefer
`insert_many`/`extend` so one atomic snapshot is written for the whole batch.

For dictionary-style hot paths, use the indexed key-value view. It uses the
declared primary-key index by default (a table without a definition defaults to
`id`) and supports an explicitly selected single-column key:

```python
users = db["users"]
users.kv[1001]                         # detached row copy, O(1) lookup
users.kv.update(1001, {"name": "Alicia"})
users.kv.set(1001, {"name": "Alice"})  # replace; key fields are filled in
users.kv.delete(1001)
users.kv.update_many({
    1002: {"name": "Bob"},
    1003: {"name": "Cara"},
})                                       # one lock and one WAL frame
rows = users.kv.get_many([1001, 1002])
events_by_token = db["events"].key_value("token")
```

`get`, `require`, `contains`, `keys`, `values`, `items`, `get_many`,
`update_many` and `delete_many` are also available. `set(..., upsert=True)`
and `update(..., upsert=True)` insert a missing key atomically. Returned rows
are deep copies, so mutating them cannot bypass the table lock or WAL. Key
columns are immutable through this view; changing one raises `ValueError` and
prevents stale index entries.

Small append batches and equality-based or primary-key CRUD operations use a
durable side-car `<name>.table.wal` frame log; larger `insert_many` batches use
one atomic full snapshot so a large reader does not have to replay a large
append frame. Each frame contains a sequence number, length, and CRC32; the
frame is flushed and `fsync`-ed before the in-process snapshot
is published. On reopen, the base snapshot and complete WAL frames are
replayed together. A torn final frame is discarded safely, while a complete
frame with a bad checksum raises `StorageError` instead of being silently
ignored. The WAL is periodically checkpointed: a complete checkpoint frame is
published before the base file and only then is the WAL atomically rotated.
This ordering keeps old-base/new-WAL and new-base/old-WAL reader combinations
recoverable during a crash. Complex predicates and physical list-position
operations continue to use the atomic full-snapshot path.

Lock waits are bounded by default. Pass `lock_timeout=None` to retain an
unbounded wait or set a positive number of seconds to receive
`StorageBusyError` when contention lasts too long.

Optional JSON definitions can be placed in `def/table` next to the data
directory. `FileMappedDatabase` automatically discovers one `<table>.json`
definition file and exposes the parsed definition
as `table.schema`. The definition enforces maximum row count, declared column
names, non-null primary keys, and primary-key uniqueness during reads and
writes. See [def/table/README.md](../def/table/README.md) for the exact JSON
format.

