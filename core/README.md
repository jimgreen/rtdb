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

Direct `Table` instances without a schema use the snapshot path. New tables
opened through a `TableDefinition` always use the fixed binary backend. The
JSON file in `def/table` is metadata only; it is not the table's row storage
format.

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
batch. Equality predicates on fixed tables use secondary slot indexes, so
these operations avoid a full snapshot rewrite:

```python
users.select({"group": "a"}, columns=["id", "name"], limit=20)
users.update({"age": 32}, {"group": "a"}, limit=1)
users.delete({"group": "inactive"})

# Several equality updates or deletes share one WAL frame and one durable
# commit. The fixed backend publishes the batch atomically.
users.update_many_where([
    ({"id": 1001}, {"name": "Alice"}),
    ({"id": 1002}, {"name": "Bob"}),
])
users.delete_many_where([{"group": "expired"}, {"group": "removed"}])
```

For code that naturally performs many point updates, the explicit write-batch
context provides the same durable group-commit boundary without constructing a
separate list first:

```python
with users.transaction() as batch:
    for user_id, name in changes:
        batch.update_by_pk(user_id, {"name": name})
```

The context accepts equality `update`, `update_by_pk`, the declared-primary-key
form of `update_by_key`, equality `delete`, `delete_by_pk`, the corresponding
declared-primary-key `delete_by_key`, `insert`, and `insert_many` calls.
One context contains one operation family so its commit is atomic.  On a
fixed table it writes one redo-WAL frame and performs one WAL `fsync`.  The
changed slots and generation header are published into the shared mapping,
then a bounded checkpoint flushes the mapping and retires the WAL. A normal
single-row method therefore returns after its WAL frame is durable, while the
table file may still have a recoverable WAL tail. This is the Redis/AOF-style
durability boundary: a crash before the checkpoint replays the CRC-checked
frames, and a crash after the checkpoint sees the new generation directly.

Each individual fixed-table write intentionally pays one durable WAL sync;
deferring the table `fsync` removes the dominant duplicate sync from a
single-row update or delete without exposing an acknowledged operation to data
loss. When the workload allows it,
`insert_many`, `update_many_where`, `delete_many_where`, and key-value
`update_many`/`delete_many` are the safe throughput path: they keep the same
WAL and CRC guarantees while committing the complete batch with one WAL sync.

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
users.kv.get(1001, fields=["id", "name"])  # projection-aware hot read
events_by_token = db["events"].key_value("token")

# Redis-style read pipeline: queue point reads, then execute one indexed
# multi-get. Results follow command order and are detached row copies.
pipe = users.kv.read_pipeline()
pipe.get(1001).contains(1002).get_many([1003, 1004])
values = pipe.execute()
```

Repeated key reads use a bounded process-local LRU decoded-row cache.  The
cache is checked against the shared table generation on every access, refreshed
for locally changed slots, and discarded when another process publishes a
generation that this process has not observed.  `get_many()` uses the same
cache while resolving all requested slots through one persisted key index, so
hot key sets avoid repeated field decoding without becoming a second source of
truth.

Primary-key projections such as `table.select({"id": 1001},
columns=["id", "name"])` use a separate bounded projection LRU.  A projection
hit returns the already decoded fields without rebuilding the key lookup or
touching the mapped record.  Entries carry both generation and slot metadata;
locally committed rows refresh only the projections that include changed
fields, while an external generation change discards untrusted entries before
reuse.  The projection cache is disposable and does not participate in
recovery.

For declared primary keys, repeated point operations also use a bounded direct
primary-key-to-slot cache.  The first access still resolves through the
authoritative equality index; later reads, key-value patches, and deletes can
address the stable mmap slot directly.  Each entry carries the table
generation and is advanced only for a local value-only commit.  Inserts,
deletes, primary-key changes, or an externally observed generation invalidate
the affected entries, so the cache never replaces the mmap/WAL source of
truth.  Composite keys are stored as one tuple key and follow the same path.

On a cold process, the first primary-key operation uses a persisted-index
probe.  It validates both redundant index headers, metadata, payload CRC,
slot bounds, uniqueness, and all incremental add/remove frames, then compares
the encoded key directly.  Unrelated index keys are kept as compact encoded
bytes instead of being decoded into Python dictionaries; the resulting probe
map is reused for the observed generation and advanced across value-only
commits.  If the side file is stale, torn, or structurally invalid, the code
falls back to the authoritative mmap scan/full index loader.  This shortens
cold point lookup without changing the WAL, generation, or hot-synchronisation
rules.  When several reader threads arrive at a cold generation together, an
adaptive single-flight fallback builds the full equality index once instead of
serialising every worker behind the encoded-key probe; a single-threaded cold
lookup still uses the direct probe.

The fixed backend also publishes a separate primary-key probe sidecar named
`<name>.table.fixedpk.<digest>.bin`. Its payload is a CRC-checked, fixed-width
sorted key/slot array, so a cold point read uses binary search without
materialising the complete Python equality map. Structural inserts and deletes
append fixed-width add/remove frames; multi-row changes are compacted back to a
new sorted base when the delta stream reaches 512 KiB. A one-row structural
change may leave this disposable sidecar one generation behind to avoid adding
file I/O to the hottest append path; the generation check then falls back to
the regular equality index and rebuilds the sidecar safely. Value-only commits
advance the redundant sidecar header in place when its base is current. The
sidecar is never used as a recovery source and any stale, torn, CRC-invalid or
layout-mismatched copy is ignored.

Value-only commits advance only the equality sidecars that have served an
actual lookup or update in the current process (plus an active primary
probe/slot cache). An index built internally for insert uniqueness checking is
not treated as a hot sidecar. An unused sidecar is deliberately left at its
previous generation, so the hot write path does not open or rewrite unrelated
index files. A later reader rejects that disposable stale generation and
rebuilds from the mapped table when needed.
Primary sidecar generation updates reuse one unbuffered process-local handle;
the table lock, dual headers, CRC checks, and WAL-before-publish ordering are
unchanged.

`table.cache_stats()` returns process-local row-cache, projection-cache,
primary-slot, and equality-index hit/miss counters. These counters are
diagnostics only and are never written to the WAL. A read pipeline is read-only; use
`table.transaction()` for the explicit durable group-commit boundary for
batched writes.

For a composite primary key, omit the `key_column` argument and pass all key
parts in the declared column order:

```python
links = db["links"]
links.kv[("site-a", 7)]
links.kv.update(("site-a", 7), {"state": "down"})
links.kv.delete_many([("site-a", 7), ("site-b", 3)])
```

`get`, `require`, `contains`, `keys`, `values`, `items`, `get_many`,
`read_pipeline`, `cache_stats`, `update_many` and `delete_many` are also
available. `set(..., upsert=True)`
and `update(..., upsert=True)` insert a missing key atomically. Returned rows
are deep copies, so mutating them cannot bypass the table lock or WAL. Key
columns are immutable through this view; changing one raises `ValueError` and
prevents stale index entries.

Fixed-table append, primary-key CRUD, equality CRUD, and batch operations use the
binary side-car `<name>.table.fixedwal` frame log. Each frame contains a
sequence number, target slots, fixed records, length, and CRC32. The frame is
flushed and `fsync`-ed first; the changed slots and redundant generation
header are then published into the mmap without waiting for a table sync.
When the stream reaches `_FIXED_WAL_CHECKPOINT_BYTES` (4 MiB by default), one
checkpoint flushes the dirty mapping, `fsync`-s the table, and truncates the
covered WAL. On reopen, complete frames are replayed together and a torn final
frame is discarded safely. A complete frame with a bad checksum raises
`StorageError` instead of being silently ignored. Full list-style rewrites also
use this in-place binary WAL path, keeping the file identity stable for
concurrent readers. Complex predicates still decode a snapshot, while
primary-key and key-value paths remain slot-oriented.

Readers do not replay a retained WAL on every access. They compare the mapped
generation and WAL file size with process-local observations, and inspect only
the last frame when either changes. A tail generation already covered by the
table header is safe to skip; a newer or torn tail enters the locked full
recovery path. This preserves hot synchronization between already-open
processes while bounding both read overhead and crash recovery work.

The fixed backend reuses one process-local table mapping, WAL handle, and lock
handle instead of reopening them for every short operation. Recovery checks
use an independent WAL metadata-size query, so concurrent readers do not race
on a shared seek position. Equality indexes retain insertion order, and a
patch that does not change a key rewrites only
the touched fixed-width fields before recalculating that row's CRC. These
caches are generation-checked against the shared redundant header on every
operation, so another process's append or update is visible without weakening
the WAL, CRC, lock, or `fsync` ordering.

The process-local lock handle initialises its one-byte sidecar only once per
path; every transaction still takes the OS advisory lock, while the
thread-local re-entrant lock continues to serialize same-process callers.
After a WAL handle has been positioned once, the commit path tracks its local
append offset and avoids a redundant EOF seek.  These are syscall reductions
only; the WAL frame is still flushed and synced before a write returns, while
the table mapping is flushed and synced by the bounded checkpoint.

The two redundant header slots are copied as a small shared-memory signature;
after the first CRC-checked decode, an unchanged header reuses its decoded
generation and row count. This removes repeated header CRC/unpack work while
still detecting an external commit immediately. Fixed updates also reuse the
already detached patch supplied by the public API, and immutable schema
projections and field maps are prepared once during table construction.

After a local durable publish, the new generation header is inserted directly
into the process-local header cache, so consecutive writes do not decode their
own freshly published header again.  The mmap flush is also page-targeted for
small commits: only changed record pages and the active redundant header slot
are flushed before the same table-file `fsync`; broad batches automatically use
one full-map flush to avoid excessive range calls.
Batch equality updates and key-value updates use the same fixed-field patch
path when no primary-key column changes, so a large batch avoids decoding and
re-encoding untouched columns while retaining per-record CRC validation.

Equality reads validate a candidate record only when it is part of the
requested result; the index lookup and projection share that one CRC-checked
read instead of decoding each matching slot twice.  Unordered `LIMIT`/
`OFFSET` queries stop the index walk at the end of the requested window and
recheck the generation before returning, retrying if another process commits
during the read.  Primary-key batch updates and deletes address the cached
slot map directly, while single-row deletes remove only the affected key from
current-generation indexes rather than rebuilding every bucket.

Index construction also skips empty fixed slots from the state byte and
decodes only the indexed fields while still checking the CRC of the complete
payload.  This reduces the one-time cost of opening a large table without
weakening corruption detection.

The decoded equality maps use a dedicated process-local read/write lock.
Concurrent index hits therefore share the map without waiting behind header,
snapshot or descriptor-cache maintenance; builders and commit-time map
replacement still take the exclusive side of that lock.

After that first construction, the fixed backend publishes the equality map
to a binary side file named
`<name>.table.fixedidx.<digest>.bin`.  The side file contains the indexed
column names, table layout/record-size metadata, table generation, encoded
keys and slot lists, with redundant CRC-checked headers and a CRC-checked
payload.  A new process or a reopened `Table` first checks this file and loads
the map directly; it only scans the table when the side file is missing,
corrupt, from another generation, or from another layout.  This removes the
50,000-slot rebuild from ordinary cold process starts while retaining the
record CRC and key recheck on every selected or modified slot.

The side file is an optional cache and is never required for recovery.  The
WAL `fsync` finishes before side-file maintenance; the table mapping follows
the independent checkpoint policy.  A value update that does not touch an
indexed column advances the side-file generation header without rewriting its
payload.  Inserts and deletes append compact
add/remove delta frames, so a single-row structural update remains O(1) in the
side file; once the delta stream reaches 4 MiB it is compacted into a fresh
base map.  Key changes and snapshot replacement invalidate affected side
files.  Any side-file write failure is ignored and the next access safely
rebuilds it from the table.

短事务的侧索引维护还复用进程内的 `r+b` 文件句柄和已经解码的描述符。每次
原地更新前仍由表锁串行化，侧文件被 `os.replace` 原子替换时先关闭旧句柄；
因此 Windows 下不会把旧文件句柄写到新索引路径，也不会把句柄泄漏到临时目录
清理。这个优化只减少打开、关闭和元数据解码系统调用，侧索引的双 header、
metadata、payload CRC 和 generation 校验保持不变；索引损坏或版本不一致时仍
回退到 mmap 权威数据并重建。

This is the fixed backend's hot-synchronization guarantee: after one process's
durable commit returns, another process may keep using an already-open
`FileMappedDatabase`/`Table` object and immediately read the new generation.
The shared `mmap` exposes the published slots, while the generation check
invalidates stale row and equality-index caches; reopening the database or
calling a refresh method is not required. A read that overlaps a commit may
observe either the previous or the next complete generation. If it catches a
slot while that slot is being written, the record CRC rejects the mixed bytes
instead of returning them as a valid row. The regression
`test_hot_sync_reuses_open_table_after_external_process_update` exercises a
cross-process update after priming both caches.

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

## Redis backend

`core/redis_db.py` provides `RedisDatabase`/`RedisTable` with the same main
relation, list, key-value, SQL and read-pipeline operations:

```python
from core import RedisDatabase

with RedisDatabase(
    r"D:\codex\rtdb\dat",
    redis_url="redis://127.0.0.1:6379/0",
    schema_directory=r"D:\codex\rtdb\def\table",
) as db:
    table = db["records"]
    table.insert_many(rows)
    table.update_by_pk(42, {"value": 100})
    row = table.kv.get(42)
    rows = table.select({"group": 3}, order_by="id DESC", limit=20)
```

Install the optional client dependencies with
`python -m pip install -r core/requirements-redis.txt`. This backend requires a
standalone Redis 7 or later server and a JSON table definition with primary
keys. The directory determines the Redis namespace; rows live in Redis, and
only table definitions use JSON. Different processes must use the same Redis
server/database, directory identity, and prefix to share a table.

Each row is a Redis hash containing binary MessagePack field values. Physical
row order and each field's equality index use sorted sets; the composite
primary key is encoded directly in the row key. Index membership changes are
incremental. Point and equality reads, inserts, patches, deletes and write
batches execute in Lua. Scripts validate uniqueness, schema identity
and Redis key types before publishing the complete plan. Multi-condition
queries filter candidates before applying `OFFSET`/`LIMIT`. A read pipeline
returns a single server-side snapshot, including when another client writes.

Bounded multi-condition queries with a large smallest index first check up to
128 candidates. If these satisfy the requested window, the script returns
immediately; otherwise it uses Redis `ZINTER` and filters the intersection.
This avoids copying a large candidate set for a first-row hit and avoids Lua
row loads for nonmatching candidates in sparse intersections. Queries with
earlier staged writes use shadow-aware filtering so later commands see the
same batch's updates. Unbounded reads keep the original scan strategy.
Plain ordered reads and single-equality reads without staged writes seek
directly to the requested sorted-set rank, decoding only the requested page.
All these changes run inside the same Lua operation and retain the existing
schema, generation, index validation and durable write behavior.

The independent query benchmark on 50,000 rows reduced a 20-call ordered
`OFFSET 40000 LIMIT 1` workload from 7.410220 s to 0.004970 s, and a single
equality `OFFSET 20000 LIMIT 1` workload from 3.637358 s to 0.004177 s. These
are aggregate times for 20 calls, not one-call latency. The multi-condition
`LIMIT 1` cases improved 25.62x for an early match, 4.55x for a late match and
39.48x for an empty intersection. Reproduce these figures with
`benchmarks/benchmark_redis_queries.py`; the results depend on row count,
index selectivity, network distance and Redis persistence settings.

The server is the shared concurrency point: an already-open process reads a
committed update immediately and does not need to reopen the database.
Redis-py's connection pool supports threads; each process should open its own
`RedisDatabase`. Composite primary keys are passed as tuples or lists, for
example `table.kv.get((site, device))`. Arbitrary Python predicates and physical
list reordering use an O(N) snapshot with generation-checked commit and bounded
conflict retries; prefer indexed operations for frequent point updates.
Explicit `table.transaction()` batches accept one operation family (insert,
update or delete). Redis Lua prevents interleaving but does not provide rollback
after an unexpected server-side runtime error. The namespace must be owned
exclusively by this backend, without external key edits, TTLs or eviction.

By default, `RedisDatabase(..., durability="always")` checks these server
settings when opening the database and rejects weaker configurations:

```text
appendonly yes
appendfsync always
no-appendfsync-on-rewrite no
maxmemory-policy noeviction
```

The client does not change server settings. `durability="server"` explicitly
accepts the server's existing policy, including `everysec` or disabled AOF, and
therefore is not equivalent to the default per-write durable configuration.
Keep the checked settings unchanged throughout a database session.

Use `wait_replicas=N, wait_timeout_ms=...` when replica acknowledgement is part
of the deployment contract. `WAIT` runs on the same connection as the write;
it acknowledges replication, not replica disk fsync. If too few replicas
respond, `RedisDurabilityError` (exported by `core`) means the write has already
committed. Network failures can also leave a write's outcome unknown. Transport
retries are disabled, and supplied clients must disable them as well; do not
blindly repeat an ambiguous write.

The comparable workload is in
[`benchmarks/benchmark_redis_vs_file.py`](../benchmarks/benchmark_redis_vs_file.py):

```powershell
python benchmarks\benchmark_redis_vs_file.py --rows 50000 --repeat 3 `
  --redis-url redis://127.0.0.1:6390/0 --durability always `
  --output output\redis-comparison-50000.json
```

Connection/configuration failures stop the comparison; workload or content
mismatches also fail the run. Both backends use the same operations, alternate
execution order, and verify the final rows and SHA-256 digest. The reported
ratio is `median(file_mapped_seconds) / median(redis_seconds)`; values above 1
mean Redis was faster for that workload and server/AOF configuration.

The 2026-10-28 comparison used Windows, Redis 8.10.3 (MSYS2), redis-py 8.1.0,
50,000 initial rows and three runs with the durable configuration above. Redis
was faster for 4-process individual appends (7.89x), 4-process batched appends
(2.47x), 8-thread mixed reads/writes (1.36x) and individual deletes (7.06x).
File-mapped storage was faster for local point reads and most bulk operations.
See [the benchmark report](../benchmarks/README.md#redis-与文件映射后端对比)
for workload sizes, medians, limitations and reproducible commands.

Real-server integration tests require `RTDB_TEST_REDIS_URL`; without it the
Redis integration cases are skipped. To run them against a dedicated server:

```powershell
$env:RTDB_TEST_REDIS_URL = "redis://127.0.0.1:6390/0"
python -m unittest core.test_redis_db -v
```

`benchmarks/verify_redis_durability.py --redis-server <path> --output
output/redis-durability.json` starts its own isolated server, verifies a
cross-process update through an already-open handle, forcibly terminates Redis
after acknowledged writes, and checks exact rows and indexes after AOF replay.
The observed run recovered all 100 expected rows. This verifies process-crash
recovery; physical power loss, faulty disks and filesystem damage were not
tested. Test and benchmark cleanup is limited to their own temporary namespaces;
no `FLUSHDB` or changes to a shared server's configuration are performed.

## SQLite backend

`core/sqlite_db.py` provides `SQLiteDatabase`/`SQLiteTable` with the same
relation, list, key-value, SQL, read-pipeline and write-batch interface:

```python
from core import SQLiteDatabase

with SQLiteDatabase(r"D:\codex\rtdb\dat") as db:
    table = db["records"]
    table.insert_many(rows)
    table.update_by_pk(42, {"value": 100})
    row = table.kv.get(42)
    rows = table.select({"group": 3}, order_by="id DESC", limit=20)
```

Each logical table is stored in `<directory>/<table>.sqlite3`. Schema-defined
scalar fields use native SQLite `INTEGER`, `REAL` and `TEXT` columns; list,
object and JSON fields use MessagePack BLOBs. Primary keys and every declared
column have SQLite indexes. A metadata row stores the schema fingerprint and
row count, maintained by insert/delete triggers. A schema change is
rejected when an existing database file has a different fingerprint.

Every thread obtains its own SQLite connection. Each process must open its own
`SQLiteDatabase`; connections are never inherited by spawned workers. The
default settings are `journal_mode=WAL`, `synchronous=FULL`,
`busy_timeout=5000 ms`, `mmap_size=256 MiB`, `foreign_keys=ON` and
`cache_size=-8192`. `BEGIN IMMEDIATE` serializes writers; bounded whole-
transaction retries handle lock acquisition only. A callback is never replayed
after an ambiguous commit or I/O error. `SQLiteBusyError` means the write was
not started within the configured retry budget. SQLite WAL lets already-open
connections see committed changes without reopening the database; a read
transaction keeps one consistent snapshot for `get_many()` and the read
pipeline.

The fixed table's list reordering and arbitrary Python predicates use an
atomic snapshot replacement, while scalar equality CRUD uses native indexes.
Use `insert_many`, `update_many_where`, key-value `update_many` and
`table.transaction()` for group commits. `PRAGMA integrity_check` should be
included in operational checks after an unclean shutdown.

The three-backend comparison is in
[`benchmarks/benchmark_three_backends.py`](../benchmarks/benchmark_three_backends.py):

```powershell
python benchmarks\benchmark_three_backends.py `
  --redis-server output\redis-runtime\Redis-8.10.3-Windows-x64-msys2\redis-server.exe `
  --rows 50000 --repeat 3 --tests `
  --output output\three-backends-50000.json
```

Measured results and limitations are recorded in
[`benchmarks/README.md`](../benchmarks/README.md#sqlite文件映射redis三后端对比).

### SQLite 多进程集中写入

多进程持续向同一张表逐条写入时，可以使用 `SQLiteWriteProcess`。
主进程只启动一个额外的写进程，各生产进程共享 `SQLiteWriteClient` 端点，
通过本机认证 IPC 发送写请求；写进程收集同一表的连续请求，合并到一次
`BEGIN IMMEDIATE ... COMMIT`。读取仍使用各进程自己的 `SQLiteDatabase`，
已经打开的读取连接在下一次读取时可见新提交；正在进行的读事务保持原快照。

下面假定 `def/table/records.json` 已经定义了 `id/group/value/name` 四列：

```python
import multiprocessing as mp
from core import SQLiteDatabase


def producer(client, worker):
    try:
        for n in range(250):
            key = 100000 + worker * 250 + n
            # 返回 1 才表示该请求所属事务已经提交。
            client.append("records", {
                "id": key, "group": worker, "value": n, "name": f"row-{key}"
            })
    finally:
        client.close()


if __name__ == "__main__":
    mp.freeze_support()
    with SQLiteDatabase(r"D:\codex\rtdb\dat") as db:
        with db.write_process(batch_size=256, flush_interval=0.001) as writer:
            client = writer.client()
            with mp.get_context("spawn").Pool(4) as pool:
                pool.starmap(producer, [(client, n) for n in range(4)])
            print(writer.stats())
            print(db["records"].get_by_pk(100000))
```

也可直接构造 `SQLiteWriteProcess(directory, schema_directory=...)`。
同一个客户端端点可以传给多个进程、多个线程；每个进程/线程分别建立回复连接。
不要向工作进程传递 `writer` 所有者或 SQLite 连接。先结束并 join 所有生产者，
再关闭写进程。关闭会停止接受请求并排空已接收请求；关闭超时/失败会抛异常。
客户端 `close()` 只释放调用线程的 IPC 连接，端点之后可以重新连接。

所有接口均为**同步确认**，包括 `append`、`insert_many`、`update`、
`update_by_pk`、`update_by_key`、`update_many`、`delete`、`delete_by_pk`、
`delete_by_key`、`delete_many`。第一个参数是逻辑表名；主键可以是联合主键元组。
`update_many` 接收 `{key: patch}` 或 `(key, patch)` 列表，`delete_many` 接收
主键列表，也可传 `key_column=`。条件更新/删除支持标量等值条件及 `limit=`，
不接受任意 Python 谓词。关系 SQL 和列表重排继续通过原数据库接口使用。

```python
writer.update_by_pk("records", 100000, {"value": 42})
writer.update_many("records", {100001: {"value": 43}, 100002: {"value": 44}})
writer.update("records", {"value": 0}, {"group": 1}, limit=10)
writer.delete_many("records", [100001, 100002])
```

安全与性能边界：

- 始终保持 `WAL + synchronous=FULL`；仅在外层 COMMIT 成功后返回成功。
  一次批量请求内的操作全成全败。不同请求用 SAVEPOINT 隔离，重复主键、
  缺失键或类型错误不会撤销同批其他正常请求。
- 队列是内存传输，不是第二份持久化日志。写进程崩溃或 IPC 中断时，未收到成功
  回复的请求结果可能未知，不能盲目重试。`request_timeout` 默认 30 秒，从服务端
  接收请求开始计时；超时不代表取消，之后仍可能提交。该接口不提供崩溃后的
  自动重放、请求去重或恰好一次执行。已确认数据的持久性仍取决于 SQLite FULL
  及底层文件系统/设备正确兑现同步写入。
- `queue_size=1024` 限制排队请求数；队列满返回 `SQLiteBusyError` 并明确标记请求
  未被接收。`batch_size=256` 是每批请求数量上限，不是行数；单请求批量大小由
  调用方控制。`flush_interval=0.001` 是收集合并窗口，不是端到端延迟保证；
  设为 0 可只合并已经排队的请求。
- `flush()` 是已接收请求的顺序屏障，不能代替生产者的 join，也不是 WAL checkpoint。
  `stats()` 提供请求数、成功/失败数、分组提交数、最大分组及当前队列长度；
  `commits` 不包括建表初始化事务，也不等同于底层实际 fsync 系统调用次数。
- 仍是一张逻辑表一个 SQLite 文件。不同表分开提交，不提供跨文件原子事务。
  只有走该客户端的请求会集中处理；原来的直写接口继续可用。
- 主要面向持续并发小写。单生产者逐条确认会增加 IPC 和收集窗口开销；已经能够
  一次提交大批数据的场景应保留直接 `insert_many` 并按实测选择。

专项测试与性能比较：

```powershell
python -m unittest core.test_sqlite_db core.test_sqlite_writer
python benchmarks/benchmark_sqlite_writer.py --rows 50000 --repeat 3 `
  --output output/sqlite-writer-50000.json
```


## 文件映射版集中写入

`FileMappedWriteProcess` 提供可选的常驻单写进程。多个进程共享 `writer.client()`，
服务端按到达顺序，把连续访问同一张表的请求合并成一次 WAL 提交。
原有二进制表、CRC、WAL、generation 和进程锁协议保持不变；记录仅暂存在涉及的槽位，
不把整张表转换成字符串或 JSON。JSON 仍只用于表定义文件。

示例假定 `def/table/records.json` 定义了 `id/group/value/name`，并且主键 0–199 已存在：

```python
import multiprocessing as mp
from core import FileMappedDatabase


def producer(client, worker):
    try:
        for key in range(worker * 50, (worker + 1) * 50):
            # 返回 1 表示这次修改已完成持久化提交。
            client.update_by_pk("records", key, {"value": -key - 1})
    finally:
        client.close()


if __name__ == "__main__":
    mp.freeze_support()
    with FileMappedDatabase(r"D:\codex\rtdb\dat") as db:
        reader = db["records"]
        with db.write_process(batch_size=256, flush_interval=0.001) as writer:
            client = writer.client()
            with mp.get_context("spawn").Pool(4) as pool:
                pool.starmap(producer, [(client, n) for n in range(4)])
            print(writer.stats())
            print(reader.get_by_pk(0))  # 已打开的读者直接看到更新
```

也可直接构造 `FileMappedWriteProcess(directory, schema_directory=...)`。
只能把 `writer.client()` 传给子进程，不能传 writer 所有者、mmap 或数据库对象。
先结束并 join 生产者，再关闭 writer；正常关闭会停止接收并排空已接收的请求。
客户端 `close()` 释放调用线程的 IPC 代理；同一端点可在其他线程/进程继续使用。

所有写入接口均同步返回实际影响行数，第一个参数为表名：

- `append`、`insert_many`。
- `update_by_pk`、`update_by_key`、`update_many`（mapping 或 `(key, patch)` 列表）。
- `delete_by_pk`、`delete_by_key`、`delete_many`。
- `update(table, values, where, limit=...)`、`delete(table, where, limit=...)`。

主键支持联合主键元组；key-value 方法支持 `key_column=` 的唯一字段或字段组合。
条件更新/删除仅接受**非空标量等值条件**，可用 mapping 或简单 SQL 等式，不接受任意
Python 谓词、范围比较或无条件全表修改。无匹配条件返回 0；严格主键/key-value
接口遇到缺失键抛 `KeyError`。`update_by_pk` 可修改主键但检查唯一性；key-value 和
`update_many` 保持所选键不变。要求 JSON 定义、固定格式表和已声明主键。
原 SQL、列表访问和其他条件查询继续使用原接口。

提交与失败语义：

- 成功确认在 WAL fsync 和 mmap generation 发布完成之后。一个批量请求全成全败；
  独立请求拥有各自的暂存回滚点，某个请求的重复键、缺失键、容量或字段错误不会
  撤销同组其他正常请求。同一组中后面的请求可以看到前面请求的暂存修改。
- 写入仍持有原来的 OS 文件锁，普通直接写入可以共存；代价是重新同步索引缓存，
  所以同表高并发小写最好统一走集中接口。不同表分别提交，不提供跨表原子事务。
- WAL/映射发布发生 I/O 错误时，服务停止继续写入，返回结果未知；重新启动服务
  会走既有 WAL 恢复流程，不会自动重放未确认请求。已确认数据的持久性依赖底层
  文件系统/设备正确兑现 fsync；强杀恢复和故障注入测试不等于物理断电测试。
- 内存队列不是持久化消息队列。超时或断开连接后，请求可能已经提交，不能盲目重试。
  不提供请求去重或恰好一次执行。`request_timeout=30` 从服务端接收到请求开始计时，
  超时不表示取消，也不是建立 IPC 连接的超时限制。
- `queue_size=1024` 限制等待请求数；队列满抛 `StorageBusyError`，明确未接受请求。
  `batch_size=256` 限制组内请求数，并非行数或内存字节数；单请求大小由调用方控制。
  暂存 WAL 帧不超过既有 64 MiB 上限，超限请求撤销并报错。
- `flush_interval=0.001` 为最多等待新请求的合并窗口，不是端到端延迟保证。
  设为 0 只合并已经排队的请求。`flush()` 是已接收请求的顺序屏障，不是 checkpoint。
  `stats().commits` 统计有实际数据变化的 WAL 分组数，不包含建表，也不等同于所有
  fsync 系统调用次数；无变化请求不额外写入 WAL。

本机 50,000 行、4 进程最终三轮基准中，多进程主键更新约 2.69x，key-value 更新约
2.67x，逐条追加约 26.82x。**单生产者逐条写和同进程多线程共享表的混合读写继续
推荐直接接口**：后者集中写后增加跨进程缓存校验，实测约慢 11.13 倍。
更完整的工作量、耗时和限制见 `benchmarks/README.md`。

```powershell
python -m unittest core.test_file_mapped_writer core.test_file_mapped_db
python benchmarks/benchmark_file_mapped_writer.py --rows 50000 --writes 50 --repeat 3 `
  --output output/file-mapped-writer-final-50000.json
```

## 公共表定义精简

三种引擎共用的 JSON 定义不再包含 `head.max_elements` 和 `columns[].data_length`。
SQLite 和 Redis 取消定义级容量/长度校验；类型、主键唯一性与事务规则保留。
文件映射的固定布局转为引擎建表参数，已有二进制文件仍使用自身保存的布局。
参数默认值、重开行为及旧库迁移边界见 [表定义说明](../def/table/README.md)。
