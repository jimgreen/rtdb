"""Repeatable before/after benchmark for the file-mapped RTDB core.

The benchmark intentionally uses the public Table API so that backend
optimisations can be compared without changing the workload.  Run from the
repository root:

    python benchmarks/benchmark_core.py --json
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import FileMappedDatabase, TableDefinition


# Keep the benchmark large enough to expose index-scan and cache behaviour.
# The table definition below reserves additional capacity for the later batch,
# process and thread workloads.
SEED_ROWS = 50_000
POINT_READS = 300
POINT_UPDATES = 20
BATCH_ROWS = 2_000
PROCESS_WORKERS = 4
PROCESS_WRITES = 25
THREAD_WORKERS = 8
THREAD_READS = 60
THREAD_WRITES = 5
KEY_BATCH_ROWS = 500
NON_PRIMARY_UPDATES = 100
TRANSACTION_UPDATES = 500
HOT_KEY_READS = 2_000
BATCH_READ_KEYS = 500
BATCH_READ_ROUNDS = 20
PIPELINE_READ_KEYS = 500
PIPELINE_READ_ROUNDS = 20
PROJECTION_READ_ROUNDS = 2_000


def _db(root: str) -> FileMappedDatabase:
    return FileMappedDatabase(root, schema_directory=Path(root).parent / "def" / "table")


def _write_benchmark_schema(root: str) -> None:
    definition = TableDefinition.from_mapping(
        {
            "head": {"chn_name": "基准记录", "eng_name": "records"},
            "columns": [
                {"chn_name": "编号", "eng_name": "id", "primary_key": True, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "分组", "eng_name": "group", "primary_key": False, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "值", "eng_name": "value", "primary_key": False, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "名称", "eng_name": "name", "primary_key": False, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
            ],
        },
        Path(root).parent / "def" / "table",
        "records",
    )
    definition.to_json(Path(root).parent / "def" / "table", overwrite=True)


def _seed(root: str, count: int = SEED_ROWS) -> None:
    table = _db(root)["records"]
    table.insert_many(
        {"id": index, "group": index % 16, "value": index * 2, "name": f"row-{index}"}
        for index in range(count)
    )


def _timed(function):
    start = time.perf_counter()
    result = function()
    return time.perf_counter() - start, result


def _first_primary_read(root: str) -> float:
    """Time the first primary-key equality read in a fresh process/object."""

    db = _db(root)
    table = db["records"]
    try:
        elapsed, rows = _timed(lambda: table.select({"id": SEED_ROWS // 2}))
        if len(rows) != 1:
            raise AssertionError(rows)
        return elapsed
    finally:
        db.close()


def _cold_index_read(root: str) -> float:
    """Force the safe table scan so the persistent-index gain is measurable."""

    table_path = Path(root) / "records.table"
    for index_path in table_path.parent.glob(table_path.name + ".fixedidx.*.bin"):
        index_path.unlink()
    # The fixed primary probe is a separate disposable sidecar.  Remove it
    # too so this metric continues to represent a genuinely cold rebuild.
    for primary_path in table_path.parent.glob(table_path.name + ".fixedpk.*.bin"):
        primary_path.unlink()
    return _first_primary_read(root)


def _point_read(root: str) -> float:
    db = _db(root)
    table = db["records"]

    def work():
        for _ in range(POINT_READS):
            rows = table.select({"id": SEED_ROWS // 2})
            if len(rows) != 1:
                raise AssertionError(rows)

    return _timed(work)[0]


def _point_update(root: str) -> float:
    db = _db(root)
    table = db["records"]

    def work():
        for index in range(POINT_UPDATES):
            changed = table.update({"value": 900_000 + index}, {"id": SEED_ROWS // 2})
            if changed != 1:
                raise AssertionError(changed)

    return _timed(work)[0]


def _key_value_read(root: str) -> float:
    table = _db(root)["records"]
    view = table.kv

    def work():
        for _ in range(POINT_READS):
            row = view.get(SEED_ROWS // 2)
            if row is None or row["id"] != SEED_ROWS // 2:
                raise AssertionError(row)

    return _timed(work)[0]


def _batch_read(root: str, *, warm: bool) -> float:
    """Read a stable key set through the key-value multi-get pipeline."""

    table = _db(root)["records"]
    view = table.kv
    keys = list(range(BATCH_READ_KEYS))
    if warm:
        view.get_many(keys)
    store = table._fixed_store
    assert store is not None

    def work():
        for _ in range(BATCH_READ_ROUNDS):
            if not warm:
                # Keep this comparison deterministic: discard only the
                # optional decoded-row cache while retaining the authoritative
                # binary index.
                with store._row_cache_guard:
                    store._row_cache.clear()
            rows = view.get_many(keys)
            if len(rows) != len(keys):
                raise AssertionError(len(rows))

    return _timed(work)[0]


def _hot_key_read(root: str) -> float:
    table = _db(root)["records"]
    view = table.kv

    def work():
        for _ in range(HOT_KEY_READS):
            row = view.get(SEED_ROWS // 2)
            if row is None or row["id"] != SEED_ROWS // 2:
                raise AssertionError(row)

    return _timed(work)[0]


def _read_pipeline(root: str) -> float:
    """Read many independent key commands through the Redis-style pipeline."""

    view = _db(root)["records"].kv
    keys = list(range(PIPELINE_READ_KEYS))

    def work():
        for _ in range(PIPELINE_READ_ROUNDS):
            pipeline = view.read_pipeline()
            for key in keys:
                pipeline.get(key)
            rows = pipeline.execute()
            if len(rows) != len(keys) or any(row is None for row in rows):
                raise AssertionError(len(rows))

    return _timed(work)[0]


def _projection_read(root: str, *, warm: bool) -> float:
    """Measure repeated primary-key reads that return only two fields."""

    table = _db(root)["records"]
    store = table._fixed_store
    assert store is not None
    if warm:
        table.select({"id": SEED_ROWS // 2}, columns=["id", "name"])

    def work():
        for _ in range(PROJECTION_READ_ROUNDS):
            if not warm:
                with store._projection_cache_guard:
                    store._projection_cache.clear()
                with store._row_cache_guard:
                    store._row_cache.clear()
            rows = table.select({"id": SEED_ROWS // 2}, columns=["id", "name"])
            if rows != [{"id": SEED_ROWS // 2, "name": f"row-{SEED_ROWS // 2}"}]:
                raise AssertionError(rows)

    return _timed(work)[0]


def _individual_key_read(root: str) -> float:
    """Control path: the same 500 point reads issued one at a time."""

    view = _db(root)["records"].kv
    keys = list(range(PIPELINE_READ_KEYS))

    def work():
        for _ in range(PIPELINE_READ_ROUNDS):
            rows = [view.get(key) for key in keys]
            if len(rows) != len(keys) or any(row is None for row in rows):
                raise AssertionError(len(rows))

    return _timed(work)[0]


def _key_value_update(root: str) -> float:
    view = _db(root)["records"].kv

    def work():
        for index in range(POINT_UPDATES):
            if view.update(SEED_ROWS // 2, {"value": 910_000 + index}) != 1:
                raise AssertionError("key-value update did not affect one row")

    return _timed(work)[0]


def _key_value_batch_update(root: str) -> float:
    view = _db(root)["records"].kv
    updates = {index: {"value": 920_000 + index} for index in range(KEY_BATCH_ROWS)}
    elapsed, changed = _timed(lambda: view.update_many(updates))
    if changed != KEY_BATCH_ROWS:
        raise AssertionError(changed)
    return elapsed


def _condition_batch_update(root: str) -> float:
    table = _db(root)["records"]
    updates = {index: {"value": 930_000 + index} for index in range(KEY_BATCH_ROWS)}

    def work():
        for key, values in updates.items():
            if table.update(values, {"id": key}) != 1:
                raise AssertionError(key)

    return _timed(work)[0]


def _condition_batch_update_many(root: str) -> float:
    table = _db(root)["records"]
    updates = [
        ({"id": index}, {"value": 935_000 + index})
        for index in range(KEY_BATCH_ROWS)
    ]
    elapsed, changed = _timed(lambda: table.update_many_where(updates))
    if changed != KEY_BATCH_ROWS:
        raise AssertionError(changed)
    return elapsed


def _non_primary_condition_update(root: str) -> float:
    """Update one row at a time through a secondary equality condition."""

    table = _db(root)["records"]

    def work():
        for index in range(NON_PRIMARY_UPDATES):
            changed = table.update(
                {"value": 940_000 + index},
                {"group": index % 16},
                limit=1,
            )
            if changed != 1:
                raise AssertionError(changed)

    return _timed(work)[0]


def _transaction_update(root: str) -> float:
    """Update many primary keys through the explicit group-commit API."""

    table = _db(root)["records"]

    def work():
        with table.transaction() as batch:
            for index in range(TRANSACTION_UPDATES):
                batch.update_by_pk(index, {"value": 936_000 + index})

    elapsed, _ = _timed(work)
    return elapsed


def _batch_insert(root: str) -> float:
    db = _db(root)
    table = db["records"]
    rows = [
        {"id": SEED_ROWS + index, "group": index % 16, "value": index, "name": f"batch-{index}"}
        for index in range(BATCH_ROWS)
    ]
    elapsed, inserted = _timed(lambda: table.insert_many(rows))
    if inserted != BATCH_ROWS:
        raise AssertionError(inserted)
    return elapsed


def _process_worker(arguments: tuple[str, int, int]) -> None:
    root, start, count = arguments
    table = _db(root)["records"]
    for index in range(start, start + count):
        table.append({"id": 100_000 + index, "group": index % 16, "value": index, "name": f"proc-{index}"})


def _process_batch_worker(arguments: tuple[str, int, int]) -> None:
    root, start, count = arguments
    table = _db(root)["records"]
    table.insert_many(
        {"id": 110_000 + index, "group": index % 16, "value": index, "name": f"proc-batch-{index}"}
        for index in range(start, start + count)
    )


def _multi_process_append(root: str) -> float:
    context = mp.get_context("spawn")
    arguments = [
        (root, worker * PROCESS_WRITES, PROCESS_WRITES)
        for worker in range(PROCESS_WORKERS)
    ]

    def work():
        with context.Pool(PROCESS_WORKERS) as pool:
            pool.map(_process_worker, arguments)

    elapsed, _ = _timed(work)
    expected = SEED_ROWS + BATCH_ROWS + PROCESS_WORKERS * PROCESS_WRITES
    actual = len(_db(root)["records"])
    if actual != expected:
        raise AssertionError((actual, expected))
    return elapsed


def _multi_process_append_batched(root: str) -> float:
    context = mp.get_context("spawn")
    arguments = [
        (root, worker * PROCESS_WRITES, PROCESS_WRITES)
        for worker in range(PROCESS_WORKERS)
    ]

    def work():
        with context.Pool(PROCESS_WORKERS) as pool:
            pool.map(_process_batch_worker, arguments)

    elapsed, _ = _timed(work)
    expected = SEED_ROWS + BATCH_ROWS + PROCESS_WORKERS * PROCESS_WRITES * 2
    actual = len(_db(root)["records"])
    if actual != expected:
        raise AssertionError((actual, expected))
    return elapsed


def _thread_worker(table, worker: int) -> tuple[int, int]:
    reads = 0
    writes = 0
    base = 200_000 + worker * THREAD_WRITES
    for index in range(THREAD_READS):
        rows = table.select({"id": SEED_ROWS // 2})
        if len(rows) != 1:
            raise AssertionError(rows)
        reads += 1
    for index in range(THREAD_WRITES):
        table.append({"id": base + index, "group": worker, "value": index, "name": f"thread-{worker}-{index}"})
        writes += 1
    return reads, writes


def _multi_thread_read_write(root: str) -> float:
    db = _db(root)
    table = db["records"]

    def work():
        with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as executor:
            return list(executor.map(lambda worker: _thread_worker(table, worker), range(THREAD_WORKERS)))

    elapsed, result = _timed(work)
    reads = sum(item[0] for item in result)
    writes = sum(item[1] for item in result)
    if reads != THREAD_WORKERS * THREAD_READS or writes != THREAD_WORKERS * THREAD_WRITES:
        raise AssertionError((reads, writes))
    return elapsed


def run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="rtdb-benchmark-") as directory:
        root = str(Path(directory) / "dat")
        _write_benchmark_schema(root)
        _seed(root)
        cold_index_read = _cold_index_read(root)
        persisted_index_read = _first_primary_read(root)
        point_read = _point_read(root)
        key_value_read = _key_value_read(root)
        batch_read_cold = _batch_read(root, warm=False)
        batch_read_hot = _batch_read(root, warm=True)
        hot_key_read = _hot_key_read(root)
        read_pipeline = _read_pipeline(root)
        projection_read_cold = _projection_read(root, warm=False)
        projection_read_hot = _projection_read(root, warm=True)
        individual_key_read = _individual_key_read(root)
        point_update = _point_update(root)
        key_value_update = _key_value_update(root)
        condition_batch_update = _condition_batch_update(root)
        condition_batch_update_many = _condition_batch_update_many(root)
        transaction_update = _transaction_update(root)
        non_primary_condition_update = _non_primary_condition_update(root)
        key_value_batch_update = _key_value_batch_update(root)
        batch_insert = _batch_insert(root)
        process_append = _multi_process_append(root)
        process_append_batched = _multi_process_append_batched(root)
        thread_read_write = _multi_thread_read_write(root)
        return {
            "parameters": {
                "seed_rows": SEED_ROWS,
                "point_reads": POINT_READS,
                "point_updates": POINT_UPDATES,
                "batch_rows": BATCH_ROWS,
                "process_workers": PROCESS_WORKERS,
                "process_writes": PROCESS_WRITES,
                "thread_workers": THREAD_WORKERS,
                "thread_reads": THREAD_READS,
                "thread_writes": THREAD_WRITES,
                "key_batch_rows": KEY_BATCH_ROWS,
                "non_primary_updates": NON_PRIMARY_UPDATES,
                "transaction_updates": TRANSACTION_UPDATES,
                "projection_read_rounds": PROJECTION_READ_ROUNDS,
            },
            "seconds": {
                "cold_index_read": cold_index_read,
                "persisted_index_read": persisted_index_read,
                "point_read": point_read,
                "key_value_read": key_value_read,
                "batch_read_cold": batch_read_cold,
                "batch_read_hot": batch_read_hot,
                "hot_key_read": hot_key_read,
                "read_pipeline": read_pipeline,
                "projection_read_cold": projection_read_cold,
                "projection_read_hot": projection_read_hot,
                "individual_key_read": individual_key_read,
                "point_update": point_update,
                "key_value_update": key_value_update,
                "condition_batch_update": condition_batch_update,
                "condition_batch_update_many": condition_batch_update_many,
                "transaction_update": transaction_update,
                "non_primary_condition_update": non_primary_condition_update,
                "key_value_batch_update": key_value_batch_update,
                "batch_insert": batch_insert,
                "multi_process_append": process_append,
                "multi_process_append_batched": process_append_batched,
                "multi_thread_read_write": thread_read_write,
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print("RTDB benchmark")
    for name, seconds in result["seconds"].items():
        print(f"{name:24s} {seconds:.6f} s")


if __name__ == "__main__":
    mp.freeze_support()
    main()
