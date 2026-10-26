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


SEED_ROWS = 5_000
POINT_READS = 300
POINT_UPDATES = 20
BATCH_ROWS = 2_000
PROCESS_WORKERS = 4
PROCESS_WRITES = 25
THREAD_WORKERS = 8
THREAD_READS = 60
THREAD_WRITES = 5
KEY_BATCH_ROWS = 500


def _db(root: str) -> FileMappedDatabase:
    return FileMappedDatabase(root, schema_directory=Path(root).parent / "def" / "table")


def _write_benchmark_schema(root: str) -> None:
    definition = TableDefinition.from_mapping(
        {
            "head": {"chn_name": "基准记录", "eng_name": "records", "max_elements": 20_000},
            "columns": [
                {"chn_name": "编号", "eng_name": "id", "primary_key": True, "data_type": "int", "data_length": 8, "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "分组", "eng_name": "group", "primary_key": False, "data_type": "int", "data_length": 8, "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "值", "eng_name": "value", "primary_key": False, "data_type": "int", "data_length": 8, "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "名称", "eng_name": "name", "primary_key": False, "data_type": "str", "data_length": 32, "disp_type": "文本", "disp_menu": ""},
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
        point_read = _point_read(root)
        key_value_read = _key_value_read(root)
        point_update = _point_update(root)
        key_value_update = _key_value_update(root)
        condition_batch_update = _condition_batch_update(root)
        key_value_batch_update = _key_value_batch_update(root)
        batch_insert = _batch_insert(root)
        process_append = _multi_process_append(root)
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
            },
            "seconds": {
                "point_read": point_read,
                "key_value_read": key_value_read,
                "point_update": point_update,
                "key_value_update": key_value_update,
                "condition_batch_update": condition_batch_update,
                "key_value_batch_update": key_value_batch_update,
                "batch_insert": batch_insert,
                "multi_process_append": process_append,
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
