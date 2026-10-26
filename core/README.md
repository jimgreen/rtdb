# File-mapped table interface

`file_mapped_db.py` provides a dependency-free relation-style data store. Each
schema-defined table is one fixed-width binary `<name>.table` file under the
database directory. Its row slots are addressed directly through `mmap`, so a
primary-key lookup or update decodes only the target record; the file does not
contain JSON row data. A side-car `<name>.table.lock` file serializes write
transactions across processes and a process-local re-entrant lock protects
writers in one process. A binary redo WAL is flushed before the changed slots
and generation header are published, allowing recovery after a process or
machine failure without exposing a partially committed row.

Tables without a schema continue to use the legacy JSON snapshot format for
backward-compatible reads. New tables created with a `TableDefinition` always
use the fixed binary backend. The JSON file in `def/table` is metadata only;
it is not the table's row storage format.

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
`IS NOT NULL`. Column and table names use Python/SQL identifier syntax.
`QueryResult` is a list with `rowcount` and `lastrowid` attributes. Equality
queries use a primary-key or `id` index when available. The explicit
`get_by_pk`, `update_by_pk`, and `delete_by_pk` methods avoid a scan for
declared primary keys. A definition may mark multiple columns as
`primary_key: true`; their declaration order is the composite-key order and
the Python key is passed as a tuple, for example
`table.get_by_pk((site_id, device_id))` or `table.kv[(site_id, device_id)]`.
Lists with the same values are accepted at the API boundary and normalized to
tuples. For many writes, prefer `insert_many`/`extend` or the key-value
`update_many` method so one lock and one WAL transaction covers the whole
batch.

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

For a composite primary key, omit the `key_column` argument and pass all key
parts in the declared column order:

```python
links = db["links"]
links.kv[("site-a", 7)]
links.kv.update(("site-a", 7), {"state": "down"})
links.kv.delete_many([("site-a", 7), ("site-b", 3)])
```

`get`, `require`, `contains`, `keys`, `values`, `items`, `get_many`,
`update_many` and `delete_many` are also available. `set(..., upsert=True)`
and `update(..., upsert=True)` insert a missing key atomically. Returned rows
are deep copies, so mutating them cannot bypass the table lock or WAL. Key
columns are immutable through this view; changing one raises `ValueError` and
prevents stale index entries.

Fixed-table append, primary-key CRUD, and key-value batch operations use the
binary side-car `<name>.table.fixedwal` frame log. Each frame contains a
sequence number, target slots, fixed records, length, and CRC32. The frame is
flushed and `fsync`-ed first; the changed slots and redundant generation
header are then published together with one table `fsync`. This keeps the
write-ahead recovery guarantee while avoiding a second table-header sync on
every row update. On reopen, complete frames are replayed together and a torn
final frame is discarded safely. A complete frame with a bad checksum raises
`StorageError` instead of being silently ignored. Full list-style rewrites
also use this in-place binary WAL path, keeping the file identity stable for
concurrent readers. Complex predicates still decode a snapshot, while
primary-key and key-value paths remain slot-oriented.

For schema-defined fixed tables, `len(table)`, `table.count()` without a
predicate, and the length of the declared-primary-key `table.kv` view read the
published row count from the table header in O(1) time; they do not decode the
whole row area.

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

