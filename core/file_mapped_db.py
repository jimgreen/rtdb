"""File-mapped, relation-style storage for small and medium sized tables.

The module deliberately uses only Python's standard library.  A
schema-defined table is stored in one fixed-width binary ``*.table`` file;
records have stable offsets and are read or updated directly through
:mod:`mmap`.  A binary redo WAL and redundant generation headers make slot
updates crash-safe.  A side-car ``*.lock`` file protects the complete
read/modify/write operation across processes, while a process-local ``RLock``
also makes nested and multi-threaded access safe.  Schema-defined tables use
the fixed-width path; direct ``Table`` instances without a definition use
the snapshot path only when explicitly requested.

The API has two layers:

* :class:`FileMappedDatabase` and :class:`Table` provide fast CRUD operations
  and list-like access.
* :meth:`FileMappedDatabase.execute` and :meth:`Table.execute` implement a
  useful SQL subset (``SELECT``, ``INSERT``, ``UPDATE`` and ``DELETE``),
  including ``WHERE``, ``ORDER BY``, ``LIMIT`` and ``OFFSET``.

Rows are mappings (normally dictionaries) containing JSON-serialisable values.
The implementation favours predictable correctness and short transactions:
writers serialize read/modify/write transactions, while readers use an
optimistic immutable-snapshot path and retry if a replacement is observed.
The in-process snapshot cache avoids reparsing unchanged base/WAL files on hot
read paths and is invalidated by their filesystem signatures.
"""

from __future__ import annotations

import ast
import atexit
import copy
import ctypes
import hashlib
import json
import math
import mmap
import multiprocessing as mp
from multiprocessing.managers import BaseManager
from multiprocessing.util import Finalize
import os
import pickle
import queue
import re
import secrets
import struct
import tempfile
import threading
import time
import uuid
import weakref
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, MutableSequence, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .table_schema import ColumnDefinition, SchemaError, SchemaRegistry, TableDefinition, TableHead

if os.name == "nt":  # pragma: no cover - exercised on Windows in CI/users' machines
    import msvcrt
    import ctypes.wintypes
else:  # pragma: no cover - the branch is covered on POSIX
    import fcntl


def _open_fixed_mapping_file(path: Path, *, create: bool = False) -> Any:
    """Open a fixed table with delete sharing for long-lived mappings.

    Python's normal ``open`` does not request ``FILE_SHARE_DELETE`` on
    Windows, so an intentionally cached mmap would keep temporary table files
    undeletable until the owning ``Table`` object is closed.  The native open
    retains the normal read/write semantics while allowing an administrator or
    test fixture to remove the path; an existing mapping remains valid until
    its handle is closed.
    """

    if os.name != "nt":
        return path.open("a+b" if create else "r+b")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.LPVOID,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(path),
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        4 if create else 3,  # OPEN_ALWAYS or OPEN_EXISTING
        0,
        None,
    )
    handle_value = ctypes.cast(handle, ctypes.c_void_p).value
    invalid_handle = ctypes.c_void_p(-1).value
    if handle_value in (None, invalid_handle):
        error = ctypes.get_last_error()
        raise OSError(error, f"cannot open fixed table {path}")
    try:
        descriptor = msvcrt.open_osfhandle(int(handle_value), os.O_RDWR | os.O_BINARY)
        return os.fdopen(descriptor, "r+b", buffering=0)
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _flush_mapped_ranges(mapping: mmap.mmap, ranges: Sequence[tuple[int, int]]) -> None:
    """Flush only dirty mmap pages covered by ``ranges``.

    ``mmap.flush()`` without a range asks the platform to scan the complete
    mapping.  Fixed-table commits normally touch one or a few records plus a
    128-byte generation slot, so align those ranges to the platform's mmap
    allocation granularity and flush them independently.  Very broad or
    highly fragmented batches use one full flush to avoid turning the
    optimisation into many syscalls.
    """

    total = len(mapping)
    if total <= 0:
        return
    granularity = max(1, int(mmap.ALLOCATIONGRANULARITY))
    aligned: list[tuple[int, int]] = []
    for offset, length in ranges:
        if length <= 0:
            continue
        start = max(0, int(offset))
        end = min(total, start + int(length))
        if end <= start:
            continue
        start -= start % granularity
        end = min(total, ((end + granularity - 1) // granularity) * granularity)
        aligned.append((start, end))
    if not aligned:
        return
    aligned.sort()
    merged: list[tuple[int, int]] = []
    for start, end in aligned:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    covered = sum(end - start for start, end in merged)
    if len(merged) > 32 or covered * 2 >= total:
        mapping.flush()
        return
    for start, end in merged:
        mapping.flush(start, end - start)


__all__ = [
    "FileMappedDatabase",
    "QueryResult",
    "TableKeyValue",
    "TableReadPipeline",
    "TableWriteBatch",
    "StorageBusyError",
    "StorageError",
    "FileMappedWriteProcess",
    "FileMappedWriteClient",
    "FileMappedWriteProcessError",
    "Table",
]


class StorageError(RuntimeError):
    """Raised when a table file is missing, truncated or otherwise corrupt."""


class StorageBusyError(StorageError):
    """Raised when a thread or process cannot acquire a table lock in time."""


class FileMappedWriteProcessError(StorageError):
    """The dedicated file-mapped writer could not complete a request."""


_MAGIC = b"RTDBMAP1"  # eight bytes
_HEADER = struct.Struct(">8sQQQ")  # magic, payload length, generation, crc32
_MAX_PAYLOAD = 512 * 1024 * 1024
_WAL_MAGIC = b"RTDBWAL1"
_WAL_HEADER = struct.Struct(">8sQI")  # magic, payload length, crc32
_WAL_END_MAGIC = b"RTDBEND1"
_WAL_TRAILER = struct.Struct(">8sQ")  # end magic, payload length
_MAX_WAL_RECORD = 64 * 1024 * 1024
_WAL_COMPACT_BYTES = 4 * 1024 * 1024
_WAL_BATCH_MAX_ROWS = 256
_IDENTIFIER = r"(?:[A-Za-z_][A-Za-z0-9_]*|`[^`]+`|\"[^\"]+\"|\[[^\]]+\])"
_IDENTIFIER_RE = re.compile(rf"^\s*(?P<name>{_IDENTIFIER})\s*$")

# Schema-defined tables use a fixed-width binary layout.  Two redundant
# headers make generation/count publication crash-safe while the row area can
# stay memory mapped and be updated in place.  Direct tables without a schema
# use the snapshot path; schema-defined tables never pass through JSON rows.
_FIXED_MAGIC = b"RTDBFIX2"
_FIXED_VERSION = 1
_FIXED_HEADER_BODY = struct.Struct(">8sHHQQQQII")
_FIXED_HEADER_CRC = struct.Struct(">I")
_FIXED_HEADER_SLOT = 128
_FIXED_DATA_OFFSET = 4096
_FIXED_SCHEMA_MAGIC = b"RTDBSCM1"
_FIXED_SCHEMA_HEADER = struct.Struct(">8sH")
_FIXED_SCHEMA_FIELD = struct.Struct(">HHBQ")
_FIXED_DEFAULT_MAX_RECORDS = 100_000
_FIXED_DEFAULT_TEXT_BYTES = 512
_FIXED_DEFAULT_OBJECT_BYTES = 16 * 1024
_FIXED_SLOT_HEADER = struct.Struct(">BQI")
_FIXED_WAL_MAGIC = b"RTDBFWL2"
_FIXED_WAL_HEADER = struct.Struct(">8sQI")
_FIXED_WAL_END_MAGIC = b"RTDBFEND"
_FIXED_WAL_TRAILER = struct.Struct(">8sQ")
_FIXED_WAL_PAYLOAD_HEADER = struct.Struct(">QQI")
_FIXED_WAL_ENTRY_HEADER = struct.Struct(">QI")
_FIXED_WAL_CHECKPOINT_BYTES = 4 * 1024 * 1024
# Equality indexes are acceleration-only side files.  They have the same
# defensive properties as the fixed table headers (redundant generations and
# CRCs), but are never part of the table's durability protocol: the table and
# fixed WAL remain the source of truth.  The payload is a compact binary map
# (encoded key values followed by slot numbers), so loading a persisted index
# does not require JSON row deserialisation.  Object-valued keys continue to
# use the existing fixed-field codec and remain CRC guarded.
_FIXED_INDEX_MAGIC = b"RTDBIDX1"
_FIXED_INDEX_VERSION = 2
_FIXED_INDEX_HEADER_BODY = struct.Struct(">8sHQIQQIIQI")
_FIXED_INDEX_HEADER_CRC = struct.Struct(">I")
_FIXED_INDEX_HEADER_SLOT = 128
_FIXED_INDEX_PAYLOAD_MAGIC = b"RTDBIP2\0"
_FIXED_INDEX_PAYLOAD_HEADER = struct.Struct(">8sQ")
_FIXED_INDEX_ENTRY_HEADER = struct.Struct(">II")
_FIXED_INDEX_SLOT = struct.Struct(">Q")
_FIXED_INDEX_DELTA_MAGIC = b"RTDBDEL1"
_FIXED_INDEX_DELTA_HEADER = struct.Struct(">8sBQI")
_FIXED_INDEX_DELTA_ADD = 1
_FIXED_INDEX_DELTA_REMOVE = 2
_FIXED_INDEX_DELTA_COMPACT_BYTES = 4 * 1024 * 1024
_FIXED_INDEX_MAX_PAYLOAD = 512 * 1024 * 1024
# A primary-key sidecar stores encoded keys in sorted fixed-width order.  It
# is an acceleration-only structure: the fixed table and WAL remain the
# authority, while this file makes a cold point lookup a CRC-checked binary
# search instead of a Python loop over every equality-index entry.
_FIXED_PRIMARY_MAGIC = b"RTDBPK01"
_FIXED_PRIMARY_VERSION = 1
_FIXED_PRIMARY_HEADER_BODY = struct.Struct(">8sHQIIIIQI")
_FIXED_PRIMARY_HEADER_CRC = struct.Struct(">I")
_FIXED_PRIMARY_HEADER_SLOT = 128
_FIXED_PRIMARY_PAYLOAD_MAGIC = b"RTDBPKP1"
_FIXED_PRIMARY_PAYLOAD_HEADER = struct.Struct(">8sII")
_FIXED_PRIMARY_ENTRY_TAIL = struct.Struct(">Q")
_FIXED_PRIMARY_DELTA_MAGIC = b"RTDBPKD1"
_FIXED_PRIMARY_DELTA_HEADER = struct.Struct(">8sBQI")
_FIXED_PRIMARY_DELTA_ADD = 1
_FIXED_PRIMARY_DELTA_REMOVE = 2
_FIXED_PRIMARY_DELTA_COMPACT_BYTES = 512 * 1024
# Bound the decoded-row hot cache so a long-lived process cannot retain an
# unbounded number of detached Python dictionaries.  The mmap and binary
# indexes remain the authoritative storage; this is only a Redis-style
# read-through acceleration layer.
_FIXED_ROW_CACHE_LIMIT = 2048
# A projection cache keeps the already decoded subset used by repeated
# ``SELECT ... WHERE primary_key = ...`` calls.  It is deliberately smaller
# than the full-row cache because each distinct field list has its own entry.
# The table file and WAL remain authoritative; this cache is disposable.
_FIXED_PROJECTION_CACHE_LIMIT = 1024
# A point lookup commonly repeats the same primary key many times.  Keep a
# bounded direct key -> slot map in addition to the general equality index so
# those calls avoid the tuple-of-slots adapter and uniqueness check.  It is an
# acceleration-only cache; generation and record-key checks still guard every
# read, and the mmap/WAL remain authoritative.
_FIXED_PRIMARY_SLOT_CACHE_LIMIT = 4096


# One RLock per lock-file path.  The OS lock is still required: a Python lock
# cannot coordinate two independent processes.
_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()
_process_lock_handles: dict[str, tuple[Any, tuple[int, int, int, int]]] = {}
_process_lock_handles_guard = threading.Lock()
_process_lock_finalizers: dict[str, Any] = {}
# A cached Windows locking handle is shared by all write transactions in this
# process (the per-path RLock serialises its users).  Initialising the one-byte
# lock file on every transaction used to add a seek/tell/write/flush sequence
# to every update.  Keep that tiny piece of handle state beside the handle;
# the file signature check in ``_process_lock_handle`` still detects an
# atomically replaced sidecar and clears this marker with the old handle.
_process_lock_file_ready: set[str] = set()
_process_lock_parent_ready: set[str] = set()


def _close_process_lock_handles() -> None:
    with _process_lock_handles_guard:
        handles = [item[0] for item in _process_lock_handles.values()]
        finalizers = list(_process_lock_finalizers.values())
        _process_lock_handles.clear()
        _process_lock_finalizers.clear()
        _process_lock_file_ready.clear()
        _process_lock_parent_ready.clear()
    for finalizer in finalizers:
        try:
            finalizer.cancel()
        except Exception:
            pass
    for handle in handles:
        try:
            handle.close()
        except Exception:
            pass


atexit.register(_close_process_lock_handles)


def _thread_lock(path: Path) -> threading.RLock:
    # Table paths are normalised once by ``Table`` construction.  Resolving
    # the same Windows path on every CRUD call performs an avoidable kernel
    # round-trip and shows up in short transactions.
    key = str(path)
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[key] = lock
        return lock


class _ReadWriteLock:
    """Small re-entrant writer/readers lock for process-local caches.

    The OS file lock remains the inter-process writer fence.  This lock only
    coordinates Python objects shared by threads in one process.  Existing
    ``with guard:`` users retain exclusive/re-entrant writer semantics, while
    hot readers can use ``with guard.read():`` concurrently.  Reader-side LRU
    access deliberately does not mutate recency; recency is updated on insert
    and eviction, following Redis's approximate-LRU principle and avoiding a
    write lock on every cache hit.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._writer: Optional[int] = None
        self._write_depth = 0
        self._readers = 0
        self._reader_counts: dict[int, int] = {}
        self._waiting_writers = 0

    def acquire_read(self) -> None:
        ident = threading.get_ident()
        with self._condition:
            if self._writer == ident:
                self._reader_counts[ident] = self._reader_counts.get(ident, 0) + 1
                return
            while self._writer is not None or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
            self._reader_counts[ident] = self._reader_counts.get(ident, 0) + 1

    def release_read(self) -> None:
        ident = threading.get_ident()
        with self._condition:
            count = self._reader_counts.get(ident, 0)
            if count <= 0:
                raise RuntimeError("read lock is not held by this thread")
            if self._writer == ident:
                if count == 1:
                    self._reader_counts.pop(ident, None)
                else:
                    self._reader_counts[ident] = count - 1
                return
            if count == 1:
                self._reader_counts.pop(ident, None)
            else:
                self._reader_counts[ident] = count - 1
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    def acquire_write(self) -> None:
        ident = threading.get_ident()
        with self._condition:
            if self._writer == ident:
                self._write_depth += 1
                return
            own_reads = self._reader_counts.get(ident, 0)
            self._waiting_writers += 1
            try:
                while self._writer is not None or self._readers > own_reads:
                    self._condition.wait()
            finally:
                self._waiting_writers -= 1
            # Keep the caller's read count while it owns the write lock.  If
            # this is a read-to-write upgrade, releasing the writer must leave
            # the outer read context valid; other readers are still blocked by
            # ``_writer`` until that context exits.
            self._writer = ident
            self._write_depth = 1

    def release_write(self) -> None:
        ident = threading.get_ident()
        with self._condition:
            if self._writer != ident:
                raise RuntimeError("write lock is not held by this thread")
            self._write_depth -= 1
            if self._write_depth:
                return
            self._writer = None
            self._condition.notify_all()

    @contextmanager
    def read(self) -> Iterator[None]:
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    def __enter__(self) -> "_ReadWriteLock":
        self.acquire_write()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release_write()


def _process_lock_handle(path: Path) -> Any:
    """Reuse one OS lock-file handle per process and table path."""

    key = str(path)
    with _process_lock_handles_guard:
        signature = _file_signature(path)
        cached = _process_lock_handles.get(key)
        handle = None if cached is None else cached[0]
        if (
            handle is None
            or getattr(handle, "closed", False)
            or cached is None
            or cached[1] != signature
        ):
            if handle is not None and not getattr(handle, "closed", False):
                handle.close()
            old_finalizer = _process_lock_finalizers.pop(key, None)
            if old_finalizer is not None:
                old_finalizer.cancel()
            handle = _open_fixed_mapping_file(path, create=True)
            _process_lock_handles[key] = (handle, _file_signature(path))
            _process_lock_finalizers[key] = Finalize(handle, handle.close, exitpriority=10)
            _process_lock_file_ready.discard(key)
        if key not in _process_lock_file_ready:
            # The cached handle is process-local and callers are serialised by
            # the path RLock, so this one-time size check is safe.  Other
            # processes may create the byte concurrently; both outcomes are
            # valid and the subsequent OS lock remains the authority.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            _process_lock_file_ready.add(key)
        return handle


class _FileLock:
    """A bounded advisory lock that works for both threads and processes."""

    def __init__(self, path: Path, timeout: Optional[float] = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self._handle: Any = None

    def __enter__(self) -> "_FileLock":
        parent = self.path.parent
        parent_key = str(parent)
        with _process_lock_handles_guard:
            parent_ready = parent_key in _process_lock_parent_ready
        if not parent_ready:
            parent.mkdir(parents=True, exist_ok=True)
            with _process_lock_handles_guard:
                _process_lock_parent_ready.add(parent_key)
        try:
            self._handle = _process_lock_handle(self.path)
        except OSError as exc:
            # A table directory may have been removed and recreated while a
            # long-lived process still holds the old path cache.  Retry the
            # one-time directory creation only when the cached parent is no
            # longer present; ordinary permission/I/O failures still surface.
            if parent_ready and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
                with _process_lock_handles_guard:
                    _process_lock_parent_ready.add(parent_key)
                try:
                    self._handle = _process_lock_handle(self.path)
                except OSError as retry_exc:
                    raise StorageError(f"cannot open table lock {self.path}: {retry_exc}") from retry_exc
            else:
                raise StorageError(f"cannot open table lock {self.path}: {exc}") from exc
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        # msvcrt.locking locks bytes starting at the current offset.  The
        # cached-handle helper has already ensured that the sidecar contains
        # one byte; only position the handle for the actual lock operation.
        if os.name == "nt":
            self._handle.seek(0)
            delay = 0.0005
            # LK_LOCK retries only ten times and then raises.  A long write
            # (or a slow network filesystem) must not turn ordinary
            # contention into a failed transaction, so retry the non-blocking
            # form until the owner releases the byte.
            while True:
                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        self._handle = None
                        raise StorageBusyError(f"timed out waiting for table lock: {self.path}")
                    time.sleep(delay)
                    delay = min(delay * 1.5, 0.002)
        else:  # pragma: no cover - selected on POSIX only
            delay = 0.0005
            while True:
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        self._handle = None
                        raise StorageBusyError(f"timed out waiting for table lock: {self.path}")
                    time.sleep(delay)
                    delay = min(delay * 1.5, 0.002)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - selected on POSIX only
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle = None


@contextmanager
def _locked(lock_path: Path, timeout: Optional[float] = 30.0) -> Iterator[None]:
    """Hold both the local and inter-process lock for one table operation."""

    thread_lock = _thread_lock(lock_path)
    acquired = thread_lock.acquire() if timeout is None else thread_lock.acquire(timeout=timeout)
    if not acquired:
        raise StorageBusyError(f"timed out waiting for thread lock: {lock_path}")
    try:
        with _FileLock(lock_path, timeout=timeout):
            yield
    finally:
        thread_lock.release()


def _unquote_identifier(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and ((value[0], value[-1]) in (("`", "`"), ('"', '"'), ("[", "]"))):
        return value[1:-1]
    return value


def _validate_identifier(value: str, *, label: str = "identifier") -> str:
    value = _unquote_identifier(value)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _split_top_level(text: str, delimiter: str = ",") -> list[str]:
    """Split on a delimiter outside quotes and parentheses."""

    parts: list[str] = []
    start = 0
    depth = 0
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses")
        elif char == delimiter and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    if quote or depth:
        raise ValueError("unbalanced quote or parentheses")
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _split_keyword(text: str, keyword: str) -> list[str]:
    """Split a boolean expression at top-level keyword occurrences."""

    upper = text.upper()
    wanted = keyword.upper()
    parts: list[str] = []
    start = 0
    depth = 0
    quote: Optional[str] = None
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth -= 1
            index += 1
            continue
        if depth == 0 and upper.startswith(wanted, index):
            before = text[index - 1] if index else " "
            after_index = index + len(wanted)
            after = text[after_index] if after_index < len(text) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                parts.append(text[start:index].strip())
                start = after_index
                index = after_index
                continue
        index += 1
    if quote or depth:
        raise ValueError("unbalanced quote or parentheses in WHERE clause")
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _strip_outer_parentheses(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        quote: Optional[str] = None
        complete = True
        for index, char in enumerate(text):
            if quote:
                if char == quote and (index == 0 or text[index - 1] != "\\"):
                    quote = None
                continue
            if char in ("'", '"'):
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    complete = False
                    break
        if complete and depth == 0:
            text = text[1:-1].strip()
        else:
            break
    return text


def _parse_scalar(token: str) -> Any:
    token = token.strip()
    if not token:
        raise ValueError("empty SQL value")
    try:
        value = ast.literal_eval(token)
    except (ValueError, SyntaxError):
        upper = token.upper()
        if upper == "NULL":
            return None
        if upper in {"TRUE", "FALSE"}:
            return upper == "TRUE"
        # SQL permits bare words as string literals in simple predicates.
        return _unquote_identifier(token)
    return value


def _parse_value_list(token: str) -> list[Any]:
    token = token.strip()
    if not (token.startswith("(") and token.endswith(")")):
        raise ValueError("IN expects a parenthesised value list")
    return [_parse_scalar(item) for item in _split_top_level(token[1:-1])]


def _value_for_field(row: Mapping[str, Any], field: str) -> Any:
    field = _unquote_identifier(field)
    # SQL clients often qualify a field as ``table.column``.
    if "." in field:
        field = field.rsplit(".", 1)[1]
    return row.get(field)


def _compile_where(expression: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]]) -> Callable[[Mapping[str, Any]], bool]:
    if expression is None:
        return lambda row: True
    if callable(expression):
        return expression
    if isinstance(expression, Mapping):
        expected = dict(expression)
        return lambda row: all(_value_for_field(row, key) == value for key, value in expected.items())
    if not isinstance(expression, str):
        raise TypeError("where must be None, a mapping, SQL text or a callable")

    expression = _strip_outer_parentheses(expression)
    disjunction = _split_keyword(expression, "OR")
    if len(disjunction) > 1:
        predicates = [_compile_where(part) for part in disjunction]
        return lambda row: any(predicate(row) for predicate in predicates)
    conjunction = _split_keyword(expression, "AND")
    if len(conjunction) > 1:
        predicates = [_compile_where(part) for part in conjunction]
        return lambda row: all(predicate(row) for predicate in predicates)

    condition = _strip_outer_parentheses(expression)
    match = re.match(
        rf"^\s*(?P<field>{_IDENTIFIER}(?:\.{_IDENTIFIER})?)\s+IS\s+(?P<not>NOT\s+)?NULL\s*$",
        condition,
        re.IGNORECASE,
    )
    if match:
        field = match.group("field")
        negate = bool(match.group("not"))
        return lambda row: (_value_for_field(row, field) is not None) if negate else (_value_for_field(row, field) is None)

    match = re.match(
        rf"^\s*(?P<field>{_IDENTIFIER}(?:\.{_IDENTIFIER})?)\s+(?P<op>LIKE|IN)\s+(?P<value>.+?)\s*$",
        condition,
        re.IGNORECASE,
    )
    if match:
        field = match.group("field")
        operator = match.group("op").upper()
        token = match.group("value")
        if operator == "IN":
            values = _parse_value_list(token)
            return lambda row: _value_for_field(row, field) in values
        pattern = _parse_scalar(token)
        if not isinstance(pattern, str):
            pattern = str(pattern)
        regex = re.compile("^" + re.escape(pattern).replace("%", ".*").replace("_", ".") + "$", re.IGNORECASE | re.DOTALL)
        return lambda row: bool(regex.match(str(_value_for_field(row, field) or "")))

    match = re.match(
        rf"^\s*(?P<field>{_IDENTIFIER}(?:\.{_IDENTIFIER})?)\s*(?P<op><>|!=|>=|<=|=|>|<)\s*(?P<value>.+?)\s*$",
        condition,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"unsupported WHERE condition: {condition!r}")
    field = match.group("field")
    operator = match.group("op")
    expected = _parse_scalar(match.group("value"))

    def compare(row: Mapping[str, Any]) -> bool:
        actual = _value_for_field(row, field)
        try:
            if operator == "=":
                return actual == expected
            if operator in {"!=", "<>"}:
                return actual != expected
            if actual is None or expected is None:
                return False
            if operator == ">":
                return actual > expected
            if operator == ">=":
                return actual >= expected
            if operator == "<":
                return actual < expected
            return actual <= expected
        except TypeError:
            return False

    return compare


def _parse_equality_mapping(expression: Any) -> Optional[dict[str, Any]]:
    """Extract a JSON-safe ``field = value [AND ...]`` predicate.

    The relation API already accepts mapping predicates.  SQL callers use a
    string, so recognising this small equality subset lets fixed tables share
    the indexed slot path without changing the general SQL predicate parser.
    ``None`` deliberately means "keep the normal snapshot path".
    """

    if not isinstance(expression, str) or not expression.strip():
        return None
    parts = _split_keyword(_strip_outer_parentheses(expression), "AND")
    if not parts or any(not part for part in parts):
        return None
    result: dict[str, Any] = {}
    for part in parts:
        match = re.match(
            rf"^\s*(?P<field>{_IDENTIFIER}(?:\.{_IDENTIFIER})?)\s*=\s*(?P<value>.+?)\s*$",
            part,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        field = _unquote_identifier(match.group("field"))
        if "." in field:
            field = field.rsplit(".", 1)[1]
        if field in result:
            # A mapping cannot represent ``field = 1 AND field = 2`` without
            # changing the predicate's meaning.
            return None
        try:
            value = _parse_scalar(match.group("value"))
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return None
        result[field] = value
    return result or None


def _parse_order_by(order_by: Optional[Union[str, Sequence[Any]]]) -> list[tuple[str, bool]]:
    if order_by is None or order_by == "":
        return []
    if isinstance(order_by, str):
        items: Sequence[Any] = _split_top_level(order_by)
    elif isinstance(order_by, Mapping):
        items = [(key, value) for key, value in order_by.items()]
    else:
        items = order_by
    parsed: list[tuple[str, bool]] = []
    for item in items:
        if isinstance(item, str):
            match = re.match(rf"^\s*(?P<field>{_IDENTIFIER}(?:\.{_IDENTIFIER})?)(?:\s+(?P<direction>ASC|DESC))?\s*$", item, re.IGNORECASE)
            if not match:
                raise ValueError(f"invalid ORDER BY item: {item!r}")
            field = match.group("field")
            descending = (match.group("direction") or "ASC").upper() == "DESC"
        elif isinstance(item, Sequence) and len(item) == 2:
            field = str(item[0])
            descending = str(item[1]).upper() in {"DESC", "-1", "TRUE"} or item[1] is True
        else:
            raise ValueError(f"invalid ORDER BY item: {item!r}")
        parsed.append((_unquote_identifier(field), descending))
    return parsed


def _sort_rows(rows: list[dict[str, Any]], order_by: Optional[Union[str, Sequence[Any]]]) -> None:
    for field, descending in reversed(_parse_order_by(order_by)):
        def key(row: Mapping[str, Any], name: str = field) -> tuple[int, str, Any]:
            value = _value_for_field(row, name)
            if value is None:
                return (1, "", "")
            # Keep incomparable JSON types deterministic instead of failing a
            # whole query when one row has a string and another has a number.
            if isinstance(value, (int, float, str, bool)):
                return (0, type(value).__name__, value)
            return (0, type(value).__name__, repr(value))

        rows.sort(key=key, reverse=descending)


def _parse_table_name(token: str) -> str:
    match = _IDENTIFIER_RE.match(token)
    if not match:
        raise ValueError(f"invalid table name: {token!r}")
    return _validate_identifier(match.group("name"), label="table name")


def _snapshot_payload(rows: list[dict[str, Any]], columns: list[str], generation: int) -> bytes:
    payload = json.dumps(
        {"version": 1, "columns": columns, "rows": rows},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    return _HEADER.pack(_MAGIC, len(payload), generation, checksum) + payload


def _file_signature(path: Path) -> tuple[int, int, int, int]:
    """Return a cache-safe signature for an atomically replaced file."""

    try:
        stat = path.stat()
    except FileNotFoundError:
        return (0, 0, 0, 0)
    return (
        int(stat.st_mtime_ns),
        int(stat.st_size),
        int(getattr(stat, "st_ctime_ns", 0)),
        int(getattr(stat, "st_ino", 0)),
    )


class _WalRecordTooLarge(ValueError):
    """Internal signal to fall back to a full snapshot for one mutation."""


def _wal_frame(record: Mapping[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            dict(record),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StorageError(f"WAL record is not JSON serialisable: {exc}") from exc
    if len(payload) > _MAX_WAL_RECORD:
        raise _WalRecordTooLarge(
            f"WAL record is {len(payload)} bytes; maximum is {_MAX_WAL_RECORD}"
        )
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    return (
        _WAL_HEADER.pack(_WAL_MAGIC, len(payload), checksum)
        + payload
        + _WAL_TRAILER.pack(_WAL_END_MAGIC, len(payload))
    )


@dataclass(frozen=True)
class _Snapshot:
    rows: list[dict[str, Any]]
    columns: list[str]
    generation: int
    cache_key: tuple[int, ...]
    indexes: dict[str, dict[Any, tuple[int, ...]]]
    primary_index: dict[tuple[Any, ...], tuple[int, ...]]


def _build_column_index(rows: Sequence[Mapping[str, Any]], column: str) -> Optional[dict[Any, tuple[int, ...]]]:
    """Build an equality index when every row has a hashable value."""

    index: dict[Any, list[int]] = {}
    for position, row in enumerate(rows):
        if column not in row:
            return None
        value = row[column]
        try:
            index.setdefault(value, []).append(position)
        except TypeError:
            return None
    return {value: tuple(positions) for value, positions in index.items()}


def _build_key_positions(
    rows: Sequence[Mapping[str, Any]],
    key_columns: Sequence[str],
) -> Optional[dict[tuple[Any, ...], tuple[int, ...]]]:
    """Build a position index for one or more key columns.

    The primary-key index has the same shape, but this helper is also used to
    replay key-value WAL records for an explicitly selected single-column key.
    ``None`` means that at least one row has a missing or unhashable key and
    therefore cannot be addressed safely through the key-value interface.
    """

    index: dict[tuple[Any, ...], list[int]] = {}
    for position, row in enumerate(rows):
        try:
            key = tuple(row[column] for column in key_columns)
            hash(key)
        except (KeyError, TypeError):
            return None
        index.setdefault(key, []).append(position)
    return {key: tuple(positions) for key, positions in index.items()}


def _build_snapshot_indexes(
    rows: Sequence[Mapping[str, Any]],
    definition: Optional[TableDefinition],
) -> tuple[dict[str, dict[Any, tuple[int, ...]]], dict[tuple[Any, ...], tuple[int, ...]]]:
    """Build the small set of indexes used by the hot equality paths.

    Primary-key columns declared in the JSON definition are indexed.  Tables
    without a definition retain the historic flexible API and opportunistically
    index an ``id`` column when it is present in every row.
    """

    columns: list[str] = list(definition.primary_keys) if definition is not None else []
    if not columns and rows and all("id" in row for row in rows):
        columns.append("id")
    indexes: dict[str, dict[Any, tuple[int, ...]]] = {}
    for column in dict.fromkeys(columns):
        built = _build_column_index(rows, column)
        if built is not None:
            indexes[column] = built

    primary_index: dict[tuple[Any, ...], list[int]] = {}
    if definition is not None and definition.primary_keys:
        for position, row in enumerate(rows):
            try:
                key = tuple(row[column] for column in definition.primary_keys)
                hash(key)
            except (KeyError, TypeError):
                primary_index.clear()
                break
            primary_index.setdefault(key, []).append(position)
    return indexes, {key: tuple(positions) for key, positions in primary_index.items()}


@dataclass(frozen=True)
class _FixedField:
    name: str
    kind: str
    width: int
    offset: int


def _fixed_kind(column: Any) -> str:
    kind = str(column.data_type).strip().lower()
    if kind in {"int", "integer", "long"}:
        return "int"
    if kind in {"float", "double", "number", "decimal"}:
        return "float"
    if kind in {"bool", "boolean"}:
        return "bool"
    if kind in {"str", "string", "text", "varchar", "char", "date", "datetime", "time"}:
        return "str"
    if kind in {"list", "array"}:
        return "list"
    if kind in {"dict", "object", "json"}:
        return "object"
    return "object"


def _fixed_fields(definition: TableDefinition, *, text_bytes: int = _FIXED_DEFAULT_TEXT_BYTES,
                  object_bytes: int = _FIXED_DEFAULT_OBJECT_BYTES,
                  stored_units: Optional[Mapping[str, int]] = None) -> tuple[tuple[_FixedField, ...], int, int]:
    fields: list[_FixedField] = []
    offset = 0
    descriptor = bytearray()
    for column in definition.columns:
        kind = _fixed_kind(column)
        if kind == "int":
            payload_width = 8
        elif kind == "float":
            payload_width = 8
        elif kind == "bool":
            payload_width = 1
        elif kind == "str":
            payload_width = max(1, int(stored_units[column.eng_name] or 64)) * 4 if stored_units is not None else ((int(text_bytes) + 3) // 4) * 4
        else:
            payload_width = max(1024, int(stored_units[column.eng_name] or 256) * 4) if stored_units is not None else max(1024, ((int(object_bytes) + 3) // 4) * 4)
        width = 1 + payload_width  # null marker plus fixed payload
        fields.append(_FixedField(column.eng_name, kind, width, offset))
        descriptor.extend(column.eng_name.encode("utf-8"))
        descriptor.extend(b"\0")
        descriptor.extend(kind.encode("ascii"))
        descriptor.extend(struct.pack(">Q", width))
        offset += width
    layout_crc = zlib.crc32(descriptor) & 0xFFFFFFFF
    return tuple(fields), offset, layout_crc


def _fixed_schema_blob(definition: TableDefinition, fields: Sequence[_FixedField]) -> bytes:
    payload = bytearray(_FIXED_SCHEMA_HEADER.pack(_FIXED_SCHEMA_MAGIC, len(definition.columns)))
    for column, physical in zip(definition.columns, fields):
        name = column.eng_name.encode("utf-8")
        data_type = column.data_type.encode("utf-8")
        if len(name) > 0xFFFF or len(data_type) > 0xFFFF:
            raise SchemaError(f"fixed schema name is too long: {column.eng_name!r}")
        # Retain the binary layout descriptor, independently of public JSON.
        units = (physical.width - 1) // 4 if physical.kind in ('str', 'list', 'object') else 0
        payload.extend(_FIXED_SCHEMA_FIELD.pack(len(name), len(data_type), 1 if column.primary_key else 0, units))
        payload.extend(name)
        payload.extend(data_type)
    if len(payload) > _FIXED_DATA_OFFSET - _FIXED_HEADER_SLOT * 2:
        raise SchemaError(f"fixed schema metadata is too large: {definition.eng_name!r}")
    return bytes(payload)


def _load_fixed_definition(path: Path, table_name: str, *, with_storage: bool = False) -> Any:
    try:
        with path.open("rb") as handle:
            handle.seek(_FIXED_HEADER_SLOT * 2)
            raw = handle.read(_FIXED_DATA_OFFSET - _FIXED_HEADER_SLOT * 2)
    except OSError as exc:
        raise StorageError(f"cannot read fixed schema metadata {path}: {exc}") from exc
    if len(raw) < _FIXED_SCHEMA_HEADER.size:
        raise StorageError(f"fixed schema metadata is truncated: {path}")
    magic, count = _FIXED_SCHEMA_HEADER.unpack(raw[:_FIXED_SCHEMA_HEADER.size])
    if magic != _FIXED_SCHEMA_MAGIC:
        raise StorageError(f"invalid fixed schema metadata: {path}")
    cursor = _FIXED_SCHEMA_HEADER.size
    columns: list[ColumnDefinition] = []
    stored_units = {}
    for _ in range(count):
        if cursor + _FIXED_SCHEMA_FIELD.size > len(raw):
            raise StorageError(f"fixed schema metadata is truncated: {path}")
        name_length, type_length, primary, width_units = _FIXED_SCHEMA_FIELD.unpack(raw[cursor : cursor + _FIXED_SCHEMA_FIELD.size])
        cursor += _FIXED_SCHEMA_FIELD.size
        end = cursor + name_length + type_length
        if end > len(raw):
            raise StorageError(f"fixed schema metadata is truncated: {path}")
        try:
            name = raw[cursor : cursor + name_length].decode("utf-8")
            data_type = raw[cursor + name_length : end].decode("utf-8")
        except UnicodeError as exc:
            raise StorageError(f"invalid fixed schema metadata: {path}") from exc
        cursor = end
        stored_units[name] = int(width_units)
        columns.append(
            ColumnDefinition(
                chn_name=name,
                eng_name=name,
                primary_key=bool(primary),
                data_type=data_type,
                disp_type=data_type,
                disp_menu="",
            )
        )
    try:
        with path.open("rb") as handle:
            header_area = handle.read(_FIXED_HEADER_SLOT * 2)
    except OSError as exc:
        raise StorageError(f"cannot read fixed table header {path}: {exc}") from exc
    candidates: list[tuple[int, int]] = []
    for offset in (0, _FIXED_HEADER_SLOT):
        header = header_area[offset : offset + _FIXED_HEADER_SLOT]
        if len(header) < _FIXED_HEADER_BODY.size + _FIXED_HEADER_CRC.size:
            continue
        body = header[:_FIXED_HEADER_BODY.size]
        checksum = _FIXED_HEADER_CRC.unpack(
            header[_FIXED_HEADER_BODY.size : _FIXED_HEADER_BODY.size + _FIXED_HEADER_CRC.size]
        )[0]
        if zlib.crc32(body) & 0xFFFFFFFF != checksum:
            continue
        magic_value, version, header_size, generation, row_count, max_records, _record_size, _field_count, _layout_crc = _FIXED_HEADER_BODY.unpack(body)
        if magic_value != _FIXED_MAGIC or version != _FIXED_VERSION or header_size != _FIXED_DATA_OFFSET or row_count > max_records:
            continue
        candidates.append((int(generation), int(max_records)))
    if not candidates:
        raise StorageError(f"fixed table headers are corrupt: {path}")
    _generation, max_records = max(candidates, key=lambda item: item[0])
    head = TableHead(chn_name=table_name, eng_name=table_name)
    definition = TableDefinition(head=head, columns=tuple(columns), path=path)
    return (definition, int(max_records), stored_units) if with_storage else definition


class _FixedRecordStore:
    """Direct mmap store for schema-defined fixed-width records.

    The row area is preallocated from engine settings and each slot has
    a stable offset.  A key lookup maps directly to a slot and decodes only the
    requested row.  Writes use a compact binary redo WAL, then publish a
    redundant generation header.  No JSON is involved in the base file or in
    this store's WAL.
    """

    def __init__(self, path: Path, definition: TableDefinition, lock_path: Path, lock_timeout: Optional[float],
                 *, max_records: int = _FIXED_DEFAULT_MAX_RECORDS,
                 text_bytes: int = _FIXED_DEFAULT_TEXT_BYTES,
                 object_bytes: int = _FIXED_DEFAULT_OBJECT_BYTES) -> None:
        self.path = path
        self.definition = definition
        self.lock_path = lock_path
        self.lock_timeout = lock_timeout
        self.fields, self.payload_size, self.layout_crc = _fixed_fields(
            definition, text_bytes=text_bytes, object_bytes=object_bytes)
        self._fields_by_name = {field.name: field for field in self.fields}
        self._column_definitions = {column.eng_name: column for column in definition.columns}
        self.record_size = _FIXED_SLOT_HEADER.size + self.payload_size
        self.max_records = int(max_records)
        if self.max_records < 1:
            raise ValueError("fixed_max_records must be positive")
        self.wal_path = path.with_suffix(path.suffix + ".fixedwal")
        self.active = False
        self._cache_guard = threading.RLock()
        # Equality-index lookups are independent of the snapshot/header
        # caches.  A dedicated RW lock lets concurrent hot readers share the
        # map while writers still publish cache replacement atomically.
        self._key_cache_guard = _ReadWriteLock()
        self._primary_slot_cache_guard = _ReadWriteLock()
        # Row values are the hottest shared read path.  Keep their lock
        # separate from index/header metadata so concurrent readers do not
        # queue behind cache maintenance or a side-index refresh.
        self._row_cache_guard = _ReadWriteLock()
        # Keep one writable mapping and its file handle for the lifetime of
        # the table object.  Opening/closing an mmap for every point lookup or
        # update is surprisingly expensive on Windows; the fixed table file
        # has a stable size and identity, so a shared mapping is safe here.
        # Cross-process writers still publish through the generation header and
        # the inter-process lock, so this cache does not change consistency.
        # Shared mmap lifetime lock.  Readers hold a shared read permit while
        # decoding a record; local publishers and close() take the write
        # permit.  Cross-process writers are still fenced by the OS lock.
        self._mapping_guard = _ReadWriteLock()
        self._mapping_handle: Any = None
        self._mapping: Optional[mmap.mmap] = None
        self._wal_handle: Any = None
        self._wal_probe_guard = threading.Lock()
        # Remember the local handle position so the next append can write at
        # the already-known offset instead of seeking to EOF.  ``None`` means
        # that the handle was opened before this process observed its current
        # size and therefore must seek once; recovery/checkpoint truncation
        # resets it to 0.
        self._wal_position: Optional[int] = None
        self._wal_observed_size: Optional[int] = None
        self._wal_observed_tail_generation: Optional[int] = None
        self._wal_observed_header_generation: Optional[int] = None
        self._wal_write_active = False
        # Side-index updates are outside the table durability boundary, but
        # value-only commits still advance their redundant generation header.
        # Re-opening the same tiny file for every commit is expensive on
        # Windows.  Keep one writable handle per index and validate its path
        # signature before reuse so an external atomic replacement never
        # leaves us writing to an obsolete inode.
        self._index_handle_guard = threading.RLock()
        self._index_handle_cache: dict[str, tuple[Any, tuple[int, int, int, int]]] = {}
        self._snapshot_cache: Optional[_Snapshot] = None
        self._key_cache: dict[tuple[str, ...], tuple[int, dict[tuple[Any, ...], tuple[int, ...]]]] = {}
        # Track index shapes that were used for an actual lookup/update.  A
        # primary index is also built internally while inserting rows for
        # uniqueness validation; that internal build alone should not force
        # every later value-only write to maintain its optional sidecar.
        self._sidecar_active_columns: set[tuple[str, ...]] = set()
        # A primary side-index payload is immutable while its generation is
        # advanced.  Cache a compact encoded-key map so a burst of distinct
        # point reads does not rescan the multi-megabyte payload for every
        # key; the header generation and payload CRC remain the validity
        # guards for hot-sync and corruption detection.
        self._primary_probe_cache: Optional[
            tuple[int, int, int, dict[bytes, tuple[int, ...]]]
        ] = None
        self._primary_probe_invalid_generation: Optional[int] = None
        self._primary_slot_cache: OrderedDict[tuple[Any, ...], tuple[int, int]] = OrderedDict()
        # Lazy index construction is single-flight per key shape inside one
        # process.  This prevents a burst of reader threads from scanning the
        # same mmap repeatedly while still allowing independent primary and
        # secondary indexes to warm concurrently.
        self._index_build_guards: dict[tuple[str, ...], threading.Lock] = {}
        self._index_build_guards_guard = threading.Lock()
        # LRU hot-row cache: cache key -> (generation, slot, decoded row).
        # Keeping the slot lets a local value-only commit refresh just the
        # affected entries instead of throwing away every hot key.
        self._row_cache: OrderedDict[
            tuple[tuple[str, ...], tuple[Any, ...]], tuple[int, int, dict[str, Any]]
        ] = OrderedDict()
        # ``get(fields=...)`` is a common hot path for API/UI callers.  Full
        # rows already use ``_row_cache``; projected rows need a separate key
        # so that a two-column read does not retain or return unrelated
        # columns.  Generation and slot are checked on every hit, and the
        # cache is cleared by local commits, so an external writer can never
        # make a same-generation stale projection visible.
        self._projection_cache_guard = _ReadWriteLock()
        self._projection_cache: OrderedDict[
            tuple[tuple[str, ...], tuple[Any, ...], tuple[str, ...]],
            tuple[int, int, dict[str, Any]],
        ] = OrderedDict()
        # Redis-style observability for the local read-through cache.  These
        # counters are deliberately outside the durability path: they are
        # process-local hints for tuning and never participate in recovery.
        # CPython's integer increments are intentionally lock-free here so a
        # hot read does not pay another mutex acquisition; ``cache_stats``
        # returns a detached snapshot for callers.
        self._stats: dict[str, int] = {
            "row_cache_hits": 0,
            "row_cache_misses": 0,
            "row_cache_evictions": 0,
            "projection_cache_hits": 0,
            "projection_cache_misses": 0,
            "projection_cache_evictions": 0,
            "get_calls": 0,
            "get_many_calls": 0,
            "generation_retries": 0,
            "index_hits": 0,
            "index_rebuilds": 0,
            "index_persisted_hits": 0,
            "primary_slot_hits": 0,
            "primary_slot_misses": 0,
            "primary_slot_evictions": 0,
        }
        # Side-index descriptors are only metadata (the table/WAL remain the
        # source of truth).  Descriptors retain their validated header
        # generation and are filtered by each caller's requested table
        # generation.  Local in-place updates advance the cached header
        # directly; a directory signature notices a lazily-created or
        # atomically replaced side file.
        self._index_descriptor_cache: Optional[
            tuple[
                tuple[int, int, int, int],
                tuple[tuple[tuple[str, ...], Path, dict[str, int]], ...],
            ]
        ] = None
        # Keep the raw redundant header slots as a cache key. The bytes are
        # still copied from the shared mapping on every read, so a commit made
        # by another process is detected immediately; when unchanged, reuse
        # the CRC-validated decoded header on hot point-read paths.
        self._header_cache: Optional[tuple[bytes, bytes, dict[str, int]]] = None

    @property
    def total_size(self) -> int:
        return _FIXED_DATA_OFFSET + self.max_records * self.record_size

    def ensure_exists_locked(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("rb") as handle:
                    magic = handle.read(len(_FIXED_MAGIC))
            except OSError as exc:
                raise StorageError(f"cannot inspect table {self.path}: {exc}") from exc
            if magic != _FIXED_MAGIC:
                self.active = False
                return
            stored, capacity, units = _load_fixed_definition(self.path, self.definition.eng_name, with_storage=True)
            def layout(d: TableDefinition) -> list[tuple[str, str, bool]]:
                return [(c.eng_name, c.data_type, c.primary_key) for c in d.columns]
            if layout(stored) != layout(self.definition):
                raise StorageError(f"fixed table layout does not match definition: {self.path}")
            self.max_records = capacity
            self.fields, self.payload_size, self.layout_crc = _fixed_fields(self.definition, stored_units=units)
            self._fields_by_name = {field.name: field for field in self.fields}
            self.record_size = _FIXED_SLOT_HEADER.size + self.payload_size
            self.active = True
            self._read_header()
            # Keep the fixed WAL handle alongside the long-lived table
            # mapping.  The handle is also used as an independent metadata
            # probe, so hot readers can use ``fstat`` instead of resolving the
            # WAL path on every generation check.  An empty handle is
            # harmless: the file remains a disposable sidecar and is still
            # truncated after each durable commit.
            self._ensure_wal_handle()
            wal_size = self._wal_size()
            header = self._read_header()
            if wal_size > 0:
                tail_frame, incomplete = self._read_wal_tail_frame(wal_size)
                if incomplete or tail_frame is None:
                    self._recover_locked()
                else:
                    tail_generation, tail_row_count, tail_entries = tail_frame
                    matches = tail_generation < header["generation"]
                    if tail_generation == header["generation"]:
                        matches = int(tail_row_count) == int(header["row_count"])
                        if matches and tail_entries:
                            with self._mapped() as (_handle, mapping):
                                for slot, record in tail_entries:
                                    raw = memoryview(mapping)[
                                        self._slot_offset(slot) : self._slot_offset(slot) + self.record_size
                                    ]
                                    try:
                                        if bytes(raw) != record:
                                            matches = False
                                            break
                                    finally:
                                        raw.release()
                    if not matches:
                        self._recover_locked()
                    else:
                        self._wal_observed_size = wal_size
                        self._wal_observed_tail_generation = tail_generation
                        self._wal_observed_header_generation = header["generation"]
            else:
                self._wal_observed_size = 0
                self._wal_observed_tail_generation = None
                self._wal_observed_header_generation = header["generation"]
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A deleted/recreated table must never inherit an old WAL or equality
        # index.  Remove disposable sidecars and start with a fresh random
        # generation as a second guard when another process still has a
        # sidecar open and Windows delays its unlink.
        try:
            self.wal_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            for index_path in self.path.parent.glob(f"{self.path.name}.fixedidx.*.bin"):
                try:
                    index_path.unlink(missing_ok=True)
                except OSError:
                    pass
            for primary_path in self.path.parent.glob(f"{self.path.name}.fixedpk.*.bin"):
                try:
                    primary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass
        self._create_file_locked(generation=secrets.randbits(31), row_count=0)
        self.active = True
        self._ensure_wal_handle()

    def _header_body(self, generation: int, row_count: int) -> bytes:
        return _FIXED_HEADER_BODY.pack(
            _FIXED_MAGIC,
            _FIXED_VERSION,
            _FIXED_DATA_OFFSET,
            int(generation),
            int(row_count),
            self.max_records,
            self.record_size,
            len(self.fields),
            self.layout_crc,
        )

    @staticmethod
    def _header_slot(body: bytes) -> bytes:
        checksum = zlib.crc32(body) & 0xFFFFFFFF
        return body + _FIXED_HEADER_CRC.pack(checksum) + bytes(_FIXED_HEADER_SLOT - len(body) - _FIXED_HEADER_CRC.size)

    def _decode_header_slot(self, data: bytes) -> Optional[dict[str, int]]:
        if len(data) < _FIXED_HEADER_BODY.size + _FIXED_HEADER_CRC.size:
            return None
        body = data[:_FIXED_HEADER_BODY.size]
        checksum = _FIXED_HEADER_CRC.unpack(data[_FIXED_HEADER_BODY.size : _FIXED_HEADER_BODY.size + _FIXED_HEADER_CRC.size])[0]
        if zlib.crc32(body) & 0xFFFFFFFF != checksum:
            return None
        magic, version, header_size, generation, row_count, max_records, record_size, field_count, layout_crc = _FIXED_HEADER_BODY.unpack(body)
        if magic != _FIXED_MAGIC or version != _FIXED_VERSION or header_size != _FIXED_DATA_OFFSET:
            return None
        if max_records != self.max_records or record_size != self.record_size or field_count != len(self.fields) or layout_crc != self.layout_crc:
            raise StorageError(f"fixed table layout does not match definition: {self.path}")
        if row_count > max_records:
            raise StorageError(f"invalid fixed table row count: {self.path}")
        return {"generation": generation, "row_count": row_count}

    def _read_header(self) -> dict[str, int]:
        try:
            with self._cache_guard:
                cached = self._header_cache
            # Read the redundant headers from the long-lived mapping.  The
            # mapping is shared with other processes, so an external publish
            # is visible immediately.  Decode both redundant copies on every
            # call: mmap writes do not reliably change Windows file mtimes, so
            # an mtime-based cache could otherwise reuse a stale row count
            # during concurrent appends.
            with self._mapping_guard.read():
                _handle, mapping = self._ensure_mapping()
                # Only the two redundant header slots are needed.  Copying
                # the whole 4 KiB metadata region on every point read adds
                # avoidable memory traffic while preserving exactly the same
                # CRC and generation validation.
                first_view = memoryview(mapping)[:_FIXED_HEADER_SLOT]
                second_view = memoryview(mapping)[_FIXED_HEADER_SLOT : 2 * _FIXED_HEADER_SLOT]
                try:
                    if cached is not None and first_view == cached[0] and second_view == cached[1]:
                        return cached[2]
                    first = bytes(first_view)
                    second = bytes(second_view)
                finally:
                    first_view.release()
                    second_view.release()
        except (OSError, ValueError, StorageError) as exc:
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"cannot read fixed table {self.path}: {exc}") from exc
        candidates = [
            self._decode_header_slot(first),
            self._decode_header_slot(second),
        ]
        valid = [candidate for candidate in candidates if candidate is not None]
        if not valid:
            raise StorageError(f"fixed table headers are corrupt: {self.path}")
        header = max(valid, key=lambda item: item["generation"])
        with self._cache_guard:
            self._header_cache = (first, second, header)
        return header

    def _cache_published_header(
        self,
        mapping: mmap.mmap,
        generation: int,
        row_count: int,
    ) -> None:
        """Cache a header that has already reached durable storage.

        A local writer already computed and CRC-validated the header before
        publishing it.  Capturing both redundant slots after the final table
        ``fsync`` avoids one duplicate header copy/CRC decode on the next
        operation from the same process.  Readers still compare the mapped
        bytes on every access, so an external writer invalidates this cache
        immediately.
        """

        first_view = memoryview(mapping)[:_FIXED_HEADER_SLOT]
        second_view = memoryview(mapping)[_FIXED_HEADER_SLOT : 2 * _FIXED_HEADER_SLOT]
        try:
            first = bytes(first_view)
            second = bytes(second_view)
        finally:
            first_view.release()
            second_view.release()
        with self._cache_guard:
            self._header_cache = (
                first,
                second,
                {"generation": int(generation), "row_count": int(row_count)},
            )

    def close(self) -> None:
        with self._mapping_guard:
            mapping = self._mapping
            handle = self._mapping_handle
            self._mapping = None
            self._mapping_handle = None
            if mapping is not None:
                mapping.close()
            if handle is not None:
                handle.close()
            with self._wal_probe_guard:
                wal_handle = self._wal_handle
                self._wal_handle = None
                self._wal_position = None
                self._wal_observed_size = None
                self._wal_observed_tail_generation = None
                self._wal_observed_header_generation = None
                if wal_handle is not None:
                    wal_handle.close()
        self._close_index_handles()
        with self._cache_guard:
            self._snapshot_cache = None
            self._header_cache = None
            self._index_descriptor_cache = None
            self._sidecar_active_columns.clear()
        with self._key_cache_guard:
            self._key_cache.clear()
        with self._cache_guard:
            self._primary_probe_cache = None
            self._primary_probe_invalid_generation = None
        with self._primary_slot_cache_guard:
            self._primary_slot_cache.clear()
        with self._index_build_guards_guard:
            self._index_build_guards.clear()
        with self._row_cache_guard:
            self._row_cache.clear()
        with self._projection_cache_guard:
            self._projection_cache.clear()

    def _close_index_handles(self) -> None:
        """Close process-local writable side-index handles."""

        with self._index_handle_guard:
            cached = tuple(self._index_handle_cache.values())
            self._index_handle_cache.clear()
        for handle, _signature in cached:
            try:
                handle.close()
            except OSError:
                pass

    def _index_update_handle(self, path: Path) -> Any:
        """Return a validated reusable ``r+b`` handle for one side index.

        Side indexes are disposable acceleration files.  Their contents are
        still CRC/generation checked by readers, so the only correctness
        requirement here is to avoid using a handle after an atomic side-file
        replacement.  The descriptor cache observes parent-directory changes
        and closes these handles before reopening the new path; in-place
        generation/header updates retain the same file identity.
        """

        key = str(path)
        with self._index_handle_guard:
            cached = self._index_handle_cache.get(key)
            if cached is not None:
                handle, _cached_signature = cached
                if not getattr(handle, "closed", False):
                    return handle
                try:
                    handle.close()
                except OSError:
                    pass
                self._index_handle_cache.pop(key, None)
            try:
                handle = _open_fixed_mapping_file(path)
            except OSError as exc:
                raise StorageError(f"cannot open fixed index {path}: {exc}") from exc
            self._index_handle_cache[key] = (handle, _file_signature(path))
            return handle

    def _remember_index_handle(self, path: Path, handle: Any) -> None:
        """Refresh the cached identity after an in-place side-index write."""

        if getattr(handle, "closed", False):
            return
        with self._index_handle_guard:
            cached = self._index_handle_cache.get(str(path))
            if cached is not None and cached[0] is handle:
                # The owning table lock serialises local writers.  Directory
                # signature changes invalidate handles when a side index is
                # atomically replaced; no path stat is needed after an
                # in-place header update.
                self._index_handle_cache[str(path)] = (handle, cached[1])

    def _invalidate_index_handle(self, path: Path) -> None:
        """Close a cached handle before ``os.replace`` publishes a new file."""

        with self._index_handle_guard:
            cached = self._index_handle_cache.pop(str(path), None)
        if cached is not None:
            try:
                cached[0].close()
            except OSError:
                pass

    def cache_stats(self) -> dict[str, int]:
        """Return process-local read-cache and index counters.

        The counters are diagnostic only.  The mmap table and fixed WAL stay
        the sole sources of truth, so clearing or losing these values cannot
        affect data correctness or recovery.
        """

        with self._cache_guard:
            stats = dict(self._stats)
        with self._key_cache_guard.read():
            stats["key_cache_size"] = len(self._key_cache)
        with self._row_cache_guard.read():
            stats["row_cache_size"] = len(self._row_cache)
        with self._projection_cache_guard.read():
            stats["projection_cache_size"] = len(self._projection_cache)
        with self._primary_slot_cache_guard.read():
            stats["primary_slot_cache_size"] = len(self._primary_slot_cache)
        return stats

    def _mark_sidecar_active(self, key_columns: Sequence[str]) -> None:
        """Remember an index shape that served a real caller operation."""

        with self._cache_guard:
            self._sidecar_active_columns.add(tuple(str(column) for column in key_columns))

    def _cache_put(
        self,
        cache_key: tuple[tuple[str, ...], tuple[Any, ...]],
        generation: int,
        slot: int,
        row: dict[str, Any],
    ) -> None:
        """Insert one decoded row into the bounded LRU."""

        with self._row_cache_guard:
            self._row_cache[cache_key] = (int(generation), int(slot), row)
            self._row_cache.move_to_end(cache_key)
            while len(self._row_cache) > _FIXED_ROW_CACHE_LIMIT:
                self._row_cache.popitem(last=False)
                self._stats["row_cache_evictions"] += 1

    def _projection_cache_put(
        self,
        cache_key: tuple[tuple[str, ...], tuple[Any, ...], tuple[str, ...]],
        generation: int,
        slot: int,
        row: dict[str, Any],
    ) -> None:
        """Insert one projected row into the bounded projection LRU."""

        with self._projection_cache_guard:
            self._projection_cache[cache_key] = (int(generation), int(slot), row)
            self._projection_cache.move_to_end(cache_key)
            while len(self._projection_cache) > _FIXED_PROJECTION_CACHE_LIMIT:
                self._projection_cache.popitem(last=False)
                self._stats["projection_cache_evictions"] += 1

    def _index_build_guard_for(self, key_columns: tuple[str, ...]) -> threading.Lock:
        with self._index_build_guards_guard:
            guard = self._index_build_guards.get(key_columns)
            if guard is None:
                guard = threading.Lock()
                self._index_build_guards[key_columns] = guard
            return guard

    def __del__(self) -> None:  # pragma: no cover - best-effort interpreter cleanup
        try:
            self.close()
        except Exception:
            # Destructors must never mask the exception that is already being
            # handled by the interpreter or a temporary-directory cleanup.
            pass

    def _ensure_mapping(self) -> tuple[Any, mmap.mmap]:
        """Return the process-local fixed-table mapping, opening it once."""

        if self._mapping is not None and self._mapping_handle is not None:
            return self._mapping_handle, self._mapping
        with self._mapping_guard:
            if self._mapping is not None and self._mapping_handle is not None:
                return self._mapping_handle, self._mapping
            handle: Any = None
            try:
                handle = _open_fixed_mapping_file(self.path)
                mapping = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_WRITE)
            except (OSError, ValueError) as exc:
                if handle is not None:
                    handle.close()
                raise StorageError(f"cannot map fixed table {self.path}: {exc}") from exc
            self._mapping_handle = handle
            self._mapping = mapping
            return handle, mapping

    def _ensure_wal_handle(self) -> Any:
        with self._mapping_guard:
            if self._wal_handle is None:
                try:
                    self._wal_handle = _open_fixed_mapping_file(self.wal_path, create=True)
                    self._wal_position = None
                except OSError as exc:
                    raise StorageError(f"cannot open fixed WAL {self.wal_path}: {exc}") from exc
            return self._wal_handle

    def _wal_size(self) -> int:
        """Return the current WAL size without sharing a seekable handle."""

        # A persistent probe handle required a process-wide mutex around
        # seek/tell, which serialized every reader on the hot path.  stat() is
        # an independent metadata query and remains safe while a writer
        # appends or truncates the WAL; recovery rechecks it under the OS lock.
        try:
            handle = self._wal_handle
            if handle is not None and not getattr(handle, "closed", False):
                return int(os.fstat(handle.fileno()).st_size)
            return int(self.wal_path.stat().st_size)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise StorageError(f"cannot inspect fixed WAL {self.wal_path}: {exc}") from exc

    def _read_wal_tail(self, size: Optional[int] = None) -> tuple[Optional[int], bool]:
        """Validate and decode only the last fixed-WAL frame."""

        if size is None:
            size = self._wal_size()
        size = int(size)
        if size <= 0:
            return None, False
        handle = self._ensure_wal_handle()
        with self._wal_probe_guard:
            try:
                if size < _FIXED_WAL_TRAILER.size:
                    return None, True
                handle.seek(size - _FIXED_WAL_TRAILER.size)
                trailer = handle.read(_FIXED_WAL_TRAILER.size)
                if len(trailer) < _FIXED_WAL_TRAILER.size:
                    return None, True
                end_magic, payload_size = _FIXED_WAL_TRAILER.unpack(trailer)
                if end_magic != _FIXED_WAL_END_MAGIC or payload_size > _MAX_WAL_RECORD:
                    return None, True
                frame_size = _FIXED_WAL_HEADER.size + int(payload_size) + _FIXED_WAL_TRAILER.size
                start = size - frame_size
                if start < 0:
                    return None, True
                handle.seek(start)
                header = handle.read(_FIXED_WAL_HEADER.size)
                if len(header) < _FIXED_WAL_HEADER.size:
                    return None, True
                magic, encoded_size, checksum = _FIXED_WAL_HEADER.unpack(header)
                if magic != _FIXED_WAL_MAGIC or encoded_size != payload_size:
                    return None, True
                payload = handle.read(int(payload_size))
                if len(payload) < int(payload_size):
                    return None, True
                if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                    return None, True
                if len(payload) < _FIXED_WAL_PAYLOAD_HEADER.size:
                    return None, True
                generation, _row_count, _count = _FIXED_WAL_PAYLOAD_HEADER.unpack(
                    payload[: _FIXED_WAL_PAYLOAD_HEADER.size]
                )
                return int(generation), False
            except OSError as exc:
                raise StorageError(f"cannot read fixed WAL tail {self.wal_path}: {exc}") from exc
            finally:
                self._wal_position = None

    def _read_wal_tail_frame(
        self,
        size: int,
    ) -> tuple[Optional[tuple[int, int, list[tuple[int, bytes]]]], bool]:
        """Read and CRC-check the final frame, including its slot records."""

        size = int(size)
        handle = self._ensure_wal_handle()
        with self._wal_probe_guard:
            try:
                if size < _FIXED_WAL_TRAILER.size:
                    return None, True
                handle.seek(size - _FIXED_WAL_TRAILER.size)
                trailer = handle.read(_FIXED_WAL_TRAILER.size)
                if len(trailer) < _FIXED_WAL_TRAILER.size:
                    return None, True
                end_magic, payload_size = _FIXED_WAL_TRAILER.unpack(trailer)
                if end_magic != _FIXED_WAL_END_MAGIC or payload_size > _MAX_WAL_RECORD:
                    return None, True
                frame_size = _FIXED_WAL_HEADER.size + int(payload_size) + _FIXED_WAL_TRAILER.size
                start = size - frame_size
                if start < 0:
                    return None, True
                handle.seek(start)
                header = handle.read(_FIXED_WAL_HEADER.size)
                if len(header) < _FIXED_WAL_HEADER.size:
                    return None, True
                magic, encoded_size, checksum = _FIXED_WAL_HEADER.unpack(header)
                if magic != _FIXED_WAL_MAGIC or encoded_size != payload_size:
                    return None, True
                payload = handle.read(int(payload_size))
                if len(payload) < int(payload_size):
                    return None, True
                if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                    return None, True
                if len(payload) < _FIXED_WAL_PAYLOAD_HEADER.size:
                    return None, True
                generation, row_count, count = _FIXED_WAL_PAYLOAD_HEADER.unpack(
                    payload[: _FIXED_WAL_PAYLOAD_HEADER.size]
                )
                cursor = _FIXED_WAL_PAYLOAD_HEADER.size
                entries: list[tuple[int, bytes]] = []
                for _ in range(count):
                    if cursor + _FIXED_WAL_ENTRY_HEADER.size > len(payload):
                        return None, True
                    slot, record_size = _FIXED_WAL_ENTRY_HEADER.unpack(
                        payload[cursor : cursor + _FIXED_WAL_ENTRY_HEADER.size]
                    )
                    cursor += _FIXED_WAL_ENTRY_HEADER.size
                    if record_size != self.record_size or cursor + record_size > len(payload):
                        return None, True
                    entries.append((slot, bytes(payload[cursor : cursor + record_size])))
                    cursor += record_size
                if cursor != len(payload):
                    return None, True
                return (int(generation), int(row_count), entries), False
            except OSError as exc:
                raise StorageError(f"cannot read fixed WAL tail frame {self.wal_path}: {exc}") from exc
            finally:
                self._wal_position = None

    def _create_file_locked(self, *, generation: int, row_count: int) -> None:
        temporary: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile("w+b", prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, delete=False) as handle:
                temporary = handle.name
                slot = self._header_slot(self._header_body(generation, row_count))
                handle.write(slot)
                handle.write(slot)
                handle.write(_fixed_schema_blob(self.definition, self.fields))
                if self.total_size:
                    handle.seek(self.total_size - 1)
                    handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            raise StorageError(f"cannot create fixed table {self.path}: {exc}") from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _index_path(self, key_columns: Sequence[str]) -> Path:
        """Return the collision-resistant side path for one equality index."""

        # Keep the key names in the file metadata as well.  The digest only
        # keeps Windows paths short and avoids unsafe characters; metadata is
        # what makes a collision harmless during load.
        token = hashlib.blake2s(
            b"\0".join(str(column).encode("utf-8") for column in key_columns),
            digest_size=12,
        ).hexdigest()
        return self.path.with_name(f"{self.path.name}.fixedidx.{token}.bin")

    def _primary_slot_path(self) -> Path:
        """Return the disposable fixed-width primary-key lookup sidecar."""

        key_columns = tuple(self.definition.primary_keys)
        token = hashlib.blake2s(
            b"\0".join(str(column).encode("utf-8") for column in key_columns),
            digest_size=12,
        ).hexdigest()
        return self.path.with_name(f"{self.path.name}.fixedpk.{token}.bin")

    @staticmethod
    def _primary_header_slot(body: bytes) -> bytes:
        checksum = zlib.crc32(body) & 0xFFFFFFFF
        return body + _FIXED_PRIMARY_HEADER_CRC.pack(checksum) + bytes(
            _FIXED_PRIMARY_HEADER_SLOT - len(body) - _FIXED_PRIMARY_HEADER_CRC.size
        )

    @staticmethod
    def _decode_primary_header_slot(data: bytes) -> Optional[dict[str, int]]:
        if len(data) < _FIXED_PRIMARY_HEADER_BODY.size + _FIXED_PRIMARY_HEADER_CRC.size:
            return None
        body = data[: _FIXED_PRIMARY_HEADER_BODY.size]
        checksum = _FIXED_PRIMARY_HEADER_CRC.unpack(
            data[
                _FIXED_PRIMARY_HEADER_BODY.size :
                _FIXED_PRIMARY_HEADER_BODY.size + _FIXED_PRIMARY_HEADER_CRC.size
            ]
        )[0]
        if zlib.crc32(body) & 0xFFFFFFFF != checksum:
            return None
        (
            magic,
            version,
            generation,
            layout_crc,
            record_size,
            max_records,
            key_width,
            count,
            payload_crc,
        ) = _FIXED_PRIMARY_HEADER_BODY.unpack(body)
        if (
            magic != _FIXED_PRIMARY_MAGIC
            or version != _FIXED_PRIMARY_VERSION
            or key_width == 0
            or count > max_records
            or count > _FIXED_INDEX_MAX_PAYLOAD
        ):
            return None
        return {
            "generation": int(generation),
            "layout_crc": int(layout_crc),
            "record_size": int(record_size),
            "max_records": int(max_records),
            "key_width": int(key_width),
            "count": int(count),
            "payload_crc": int(payload_crc),
        }

    def _encode_primary_slot_payload(
        self,
        index: Mapping[tuple[Any, ...], Sequence[int]],
    ) -> tuple[int, bytes]:
        """Encode a sorted, fixed-width key -> slot map for point lookups."""

        key_columns = tuple(self.definition.primary_keys)
        fields = tuple(self._fields_by_name[column] for column in key_columns)
        encoded: list[tuple[bytes, int]] = []
        for key, positions in index.items():
            if len(key) != len(fields) or len(positions) != 1:
                raise StorageError(f"primary index is not unique: {self.path}")
            key_blob = b"".join(
                self._encode_field(value, field)
                for value, field in zip(key, fields)
            )
            slot = int(positions[0])
            if slot < 0 or slot >= self.max_records:
                raise StorageError(f"invalid primary index slot: {self.path}")
            encoded.append((key_blob, slot))
        encoded.sort(key=lambda item: item[0])
        key_width = sum(field.width for field in fields)
        if key_width <= 0:
            raise StorageError(f"invalid primary index key width: {self.path}")
        payload = bytearray(
            _FIXED_PRIMARY_PAYLOAD_HEADER.pack(
                _FIXED_PRIMARY_PAYLOAD_MAGIC,
                key_width,
                len(encoded),
            )
        )
        for key_blob, slot in encoded:
            if len(key_blob) != key_width:
                raise StorageError(f"invalid primary index key width: {self.path}")
            payload.extend(key_blob)
            payload.extend(_FIXED_PRIMARY_ENTRY_TAIL.pack(slot))
        if len(payload) > _FIXED_INDEX_MAX_PAYLOAD:
            raise StorageError(f"primary index payload is too large: {self.path}")
        return key_width, bytes(payload)

    def _persist_primary_slot_index(
        self,
        generation: int,
        index: Mapping[tuple[Any, ...], Sequence[int]],
    ) -> None:
        """Atomically publish the cold primary lookup sidecar.

        This sidecar is deliberately independent from the equality index.  A
        torn or stale copy is rejected by its redundant header, generation,
        layout and payload CRC checks, after which the normal authoritative
        index/table path rebuilds it.
        """

        try:
            key_width, payload = self._encode_primary_slot_payload(index)
            body = _FIXED_PRIMARY_HEADER_BODY.pack(
                _FIXED_PRIMARY_MAGIC,
                _FIXED_PRIMARY_VERSION,
                int(generation),
                self.layout_crc,
                self.record_size,
                self.max_records,
                key_width,
                len(index),
                zlib.crc32(payload) & 0xFFFFFFFF,
            )
            slot = self._primary_header_slot(body)
            path = self._primary_slot_path()
            temporary: Optional[str] = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w+b", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
                ) as handle:
                    temporary = handle.name
                    handle.write(slot)
                    handle.write(slot)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._invalidate_index_handle(path)
                os.replace(temporary, path)
                temporary = None
            finally:
                if temporary:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
        except (OSError, StorageError, SchemaError, ValueError, TypeError):
            # The table/WAL are authoritative.  A sidecar failure only loses
            # the cold lookup optimisation and must not fail a durable write.
            return

    def _load_primary_slot_index(
        self,
        key: tuple[Any, ...],
        generation: int,
    ) -> tuple[bool, Optional[int]]:
        """Look up one primary key through the fixed-width sidecar."""

        key_columns = tuple(self.definition.primary_keys)
        if not key_columns or len(key) != len(key_columns):
            return False, None
        try:
            fields = tuple(self._fields_by_name[column] for column in key_columns)
            target_blob = b"".join(
                self._encode_field(value, field)
                for value, field in zip(key, fields)
            )
        except (KeyError, TypeError, ValueError, StorageError):
            return False, None
        path = self._primary_slot_path()
        try:
            with path.open("rb") as handle:
                header_area = handle.read(_FIXED_PRIMARY_HEADER_SLOT * 2)
                if len(header_area) < _FIXED_PRIMARY_HEADER_SLOT * 2:
                    return False, None
                valid = [
                    self._decode_primary_header_slot(
                        header_area[offset : offset + _FIXED_PRIMARY_HEADER_SLOT]
                    )
                    for offset in (0, _FIXED_PRIMARY_HEADER_SLOT)
                ]
                valid = [item for item in valid if item is not None]
                if not valid:
                    return False, None
                header = max(valid, key=lambda item: item["generation"])
                if (
                    header["generation"] != int(generation)
                    or header["layout_crc"] != self.layout_crc
                    or header["record_size"] != self.record_size
                    or header["max_records"] != self.max_records
                    or header["key_width"] != len(target_blob)
                ):
                    return False, None
                payload = handle.read()
                base_length = _FIXED_PRIMARY_PAYLOAD_HEADER.size + header["count"] * (
                    header["key_width"] + _FIXED_PRIMARY_ENTRY_TAIL.size
                )
                if len(payload) < base_length:
                    return False, None
                if zlib.crc32(payload) & 0xFFFFFFFF != header["payload_crc"]:
                    return False, None
        except (OSError, StorageError, ValueError, TypeError):
            return False, None
        try:
            payload_magic, key_width, count = _FIXED_PRIMARY_PAYLOAD_HEADER.unpack(
                payload[: _FIXED_PRIMARY_PAYLOAD_HEADER.size]
            )
            if (
                payload_magic != _FIXED_PRIMARY_PAYLOAD_MAGIC
                or key_width != header["key_width"]
                or count != header["count"]
            ):
                return False, None
            entry_size = key_width + _FIXED_PRIMARY_ENTRY_TAIL.size
            base_length = _FIXED_PRIMARY_PAYLOAD_HEADER.size + count * entry_size
            if len(payload) < base_length:
                return False, None
            start = 0
            end = count
            payload_offset = _FIXED_PRIMARY_PAYLOAD_HEADER.size
            while start < end:
                middle = (start + end) // 2
                offset = payload_offset + middle * entry_size
                middle_key = payload[offset : offset + key_width]
                if middle_key < target_blob:
                    start = middle + 1
                else:
                    end = middle
            if start >= count:
                slot: Optional[int] = None
            else:
                offset = payload_offset + start * entry_size
                if payload[offset : offset + key_width] != target_blob:
                    slot = None
                else:
                    slot = int(
                        _FIXED_PRIMARY_ENTRY_TAIL.unpack(
                            payload[offset + key_width : offset + entry_size]
                        )[0]
                    )
                    if slot >= self.max_records:
                        return False, None
            # Structural commits append compact fixed-width deltas.  Replay
            # only the requested key here; unrelated keys never become Python
            # objects, preserving the cold point-lookup fast path.
            cursor = base_length
            while cursor < len(payload):
                if cursor + _FIXED_PRIMARY_DELTA_HEADER.size > len(payload):
                    return False, None
                magic, operation, delta_slot, delta_key_length = _FIXED_PRIMARY_DELTA_HEADER.unpack(
                    payload[cursor : cursor + _FIXED_PRIMARY_DELTA_HEADER.size]
                )
                if (
                    magic != _FIXED_PRIMARY_DELTA_MAGIC
                    or delta_key_length != key_width
                    or cursor + _FIXED_PRIMARY_DELTA_HEADER.size + delta_key_length > len(payload)
                    or delta_slot >= self.max_records
                ):
                    return False, None
                cursor += _FIXED_PRIMARY_DELTA_HEADER.size
                delta_key = payload[cursor : cursor + delta_key_length]
                if delta_key == target_blob:
                    if operation == _FIXED_PRIMARY_DELTA_ADD:
                        if slot is not None:
                            return False, None
                        slot = int(delta_slot)
                    elif operation == _FIXED_PRIMARY_DELTA_REMOVE:
                        if slot != int(delta_slot):
                            return False, None
                        slot = None
                    else:
                        return False, None
                elif operation not in (_FIXED_PRIMARY_DELTA_ADD, _FIXED_PRIMARY_DELTA_REMOVE):
                    return False, None
                cursor += delta_key_length
            if cursor != len(payload):
                return False, None
            return True, slot
        except (IndexError, struct.error, ValueError, TypeError):
            return False, None

    @staticmethod
    def _index_metadata(key_columns: Sequence[str]) -> bytes:
        payload = bytearray()
        for column in key_columns:
            encoded = str(column).encode("utf-8")
            if len(encoded) > 0xFFFF:
                raise StorageError(f"fixed index column name is too long: {column!r}")
            payload.extend(struct.pack(">H", len(encoded)))
            payload.extend(encoded)
        return bytes(payload)

    @staticmethod
    def _index_header_slot(body: bytes) -> bytes:
        checksum = zlib.crc32(body) & 0xFFFFFFFF
        return body + _FIXED_INDEX_HEADER_CRC.pack(checksum) + bytes(
            _FIXED_INDEX_HEADER_SLOT - len(body) - _FIXED_INDEX_HEADER_CRC.size
        )

    @staticmethod
    def _decode_index_header_slot(data: bytes) -> Optional[dict[str, int]]:
        if len(data) < _FIXED_INDEX_HEADER_BODY.size + _FIXED_INDEX_HEADER_CRC.size:
            return None
        body = data[: _FIXED_INDEX_HEADER_BODY.size]
        checksum = _FIXED_INDEX_HEADER_CRC.unpack(
            data[
                _FIXED_INDEX_HEADER_BODY.size : _FIXED_INDEX_HEADER_BODY.size
                + _FIXED_INDEX_HEADER_CRC.size
            ]
        )[0]
        if zlib.crc32(body) & 0xFFFFFFFF != checksum:
            return None
        (
            magic,
            version,
            generation,
            layout_crc,
            record_size,
            max_records,
            key_count,
            metadata_length,
            payload_length,
            payload_crc,
        ) = _FIXED_INDEX_HEADER_BODY.unpack(body)
        if (
            magic != _FIXED_INDEX_MAGIC
            or version != _FIXED_INDEX_VERSION
            or key_count == 0
            or metadata_length > _FIXED_INDEX_MAX_PAYLOAD
            or payload_length > _FIXED_INDEX_MAX_PAYLOAD
        ):
            return None
        return {
            "generation": int(generation),
            "layout_crc": int(layout_crc),
            "record_size": int(record_size),
            "max_records": int(max_records),
            "key_count": int(key_count),
            "metadata_length": int(metadata_length),
            "payload_length": int(payload_length),
            "payload_crc": int(payload_crc),
        }

    def _encode_persisted_index(
        self,
        key_columns: Sequence[str],
        index: Mapping[tuple[Any, ...], Sequence[int]],
    ) -> tuple[bytes, bytes]:
        try:
            fields = tuple(self._fields_by_name[column] for column in key_columns)
        except KeyError as exc:
            raise KeyError(exc.args[0]) from exc
        payload = bytearray(_FIXED_INDEX_PAYLOAD_HEADER.pack(_FIXED_INDEX_PAYLOAD_MAGIC, len(index)))
        for key, positions in index.items():
            if len(key) != len(fields):
                raise StorageError(f"invalid fixed index key width: {self.path}")
            key_blob = b"".join(self._encode_field(value, field) for value, field in zip(key, fields))
            if len(key_blob) > 0xFFFFFFFF or len(positions) > 0xFFFFFFFF:
                raise StorageError(f"fixed index entry is too large: {self.path}")
            payload.extend(_FIXED_INDEX_ENTRY_HEADER.pack(len(key_blob), len(positions)))
            payload.extend(key_blob)
            seen: set[int] = set()
            for slot in positions:
                slot = int(slot)
                if slot < 0 or slot >= self.max_records or slot in seen:
                    raise StorageError(f"invalid fixed index slot: {self.path}")
                seen.add(slot)
                payload.extend(_FIXED_INDEX_SLOT.pack(slot))
        if len(payload) > _FIXED_INDEX_MAX_PAYLOAD:
            raise StorageError(f"fixed index payload is too large: {self.path}")
        return self._index_metadata(key_columns), bytes(payload)

    def _decode_persisted_index(
        self,
        key_columns: Sequence[str],
        payload: bytes,
    ) -> Optional[dict[tuple[Any, ...], tuple[int, ...]]]:
        try:
            fields = tuple(self._fields_by_name[column] for column in key_columns)
        except KeyError:
            return None
        if len(payload) < _FIXED_INDEX_PAYLOAD_HEADER.size:
            return None
        payload_magic, count = _FIXED_INDEX_PAYLOAD_HEADER.unpack(payload[: _FIXED_INDEX_PAYLOAD_HEADER.size])
        if payload_magic != _FIXED_INDEX_PAYLOAD_MAGIC:
            return None
        if count > self.max_records:
            return None
        cursor = _FIXED_INDEX_PAYLOAD_HEADER.size
        result: dict[tuple[Any, ...], tuple[int, ...]] = {}
        seen_slots: set[int] = set()
        try:
            for _ in range(count):
                if cursor + _FIXED_INDEX_ENTRY_HEADER.size > len(payload):
                    return None
                key_length, position_count = _FIXED_INDEX_ENTRY_HEADER.unpack(
                    payload[cursor : cursor + _FIXED_INDEX_ENTRY_HEADER.size]
                )
                cursor += _FIXED_INDEX_ENTRY_HEADER.size
                expected_key_length = sum(field.width for field in fields)
                if key_length != expected_key_length:
                    return None
                key_end = cursor + key_length
                positions_end = key_end + position_count * _FIXED_INDEX_SLOT.size
                if key_end > len(payload) or positions_end > len(payload) or position_count > self.max_records:
                    return None
                key_values: list[Any] = []
                value_cursor = cursor
                for field in fields:
                    value_end = value_cursor + field.width
                    key_values.append(self._decode_field(payload[value_cursor:value_end], field))
                    value_cursor = value_end
                key = tuple(key_values)
                try:
                    hash(key)
                except TypeError:
                    return None
                positions: list[int] = []
                for position in range(position_count):
                    slot = _FIXED_INDEX_SLOT.unpack(
                        payload[key_end + position * _FIXED_INDEX_SLOT.size : key_end + (position + 1) * _FIXED_INDEX_SLOT.size]
                    )[0]
                    if slot >= self.max_records or slot in seen_slots:
                        return None
                    seen_slots.add(slot)
                    positions.append(int(slot))
                result[key] = tuple(positions)
                cursor = positions_end
            # Structural commits append compact delta frames instead of
            # serialising this complete map again.  Replay them in order,
            # retaining insertion order in each equality bucket.
            while cursor < len(payload):
                if cursor + _FIXED_INDEX_DELTA_HEADER.size > len(payload):
                    return None
                magic, operation, slot, key_length = _FIXED_INDEX_DELTA_HEADER.unpack(
                    payload[cursor : cursor + _FIXED_INDEX_DELTA_HEADER.size]
                )
                if magic != _FIXED_INDEX_DELTA_MAGIC:
                    return None
                cursor += _FIXED_INDEX_DELTA_HEADER.size
                expected_key_length = sum(field.width for field in fields)
                if key_length != expected_key_length or cursor + key_length > len(payload):
                    return None
                key_values: list[Any] = []
                value_cursor = cursor
                for field in fields:
                    value_end = value_cursor + field.width
                    key_values.append(self._decode_field(payload[value_cursor:value_end], field))
                    value_cursor = value_end
                key = tuple(key_values)
                if slot >= self.max_records:
                    return None
                try:
                    hash(key)
                except TypeError:
                    return None
                positions = result.get(key, ())
                if operation == _FIXED_INDEX_DELTA_ADD:
                    if slot in positions:
                        return None
                    result[key] = tuple(positions) + (int(slot),)
                    if slot in seen_slots:
                        return None
                    seen_slots.add(int(slot))
                elif operation == _FIXED_INDEX_DELTA_REMOVE:
                    if slot not in positions:
                        return None
                    remaining = tuple(position for position in positions if position != slot)
                    if remaining:
                        result[key] = remaining
                    else:
                        result.pop(key, None)
                    seen_slots.discard(int(slot))
                else:
                    return None
                cursor += key_length
        except (IndexError, struct.error, StorageError, UnicodeError, ValueError, TypeError):
            return None
        if cursor != len(payload):
            return None
        return result

    def _load_persisted_key_index(
        self,
        key_columns: Sequence[str],
        generation: int,
    ) -> Optional[dict[tuple[Any, ...], tuple[int, ...]]]:
        """Load a current side index, returning ``None`` for any stale/corrupt file."""

        path = self._index_path(key_columns)
        try:
            with path.open("rb") as handle:
                header_area = handle.read(_FIXED_INDEX_HEADER_SLOT * 2)
                if len(header_area) < _FIXED_INDEX_HEADER_SLOT * 2:
                    return None
                candidates = [
                    self._decode_index_header_slot(header_area[offset : offset + _FIXED_INDEX_HEADER_SLOT])
                    for offset in (0, _FIXED_INDEX_HEADER_SLOT)
                ]
                valid = [candidate for candidate in candidates if candidate is not None]
                if not valid:
                    return None
                header = max(valid, key=lambda item: item["generation"])
                if (
                    header["generation"] != int(generation)
                    or header["layout_crc"] != self.layout_crc
                    or header["record_size"] != self.record_size
                    or header["max_records"] != self.max_records
                    or header["key_count"] != len(tuple(key_columns))
                ):
                    return None
                metadata = handle.read(header["metadata_length"])
                if metadata != self._index_metadata(key_columns):
                    return None
                payload = handle.read(header["payload_length"])
                if len(payload) != header["payload_length"]:
                    return None
                if zlib.crc32(payload) & 0xFFFFFFFF != header["payload_crc"]:
                    return None
        except (OSError, StorageError, ValueError, TypeError):
            return None
        return self._decode_persisted_index(key_columns, payload)

    def _lookup_persisted_primary_slot(
        self,
        key: tuple[Any, ...],
        generation: int,
    ) -> tuple[bool, Optional[int]]:
        """Look up one primary key without materialising the complete index.

        A newly opened process used to decode every entry in the persisted
        primary-key index before serving its first point read or update.  The
        index is an acceleration cache, so a point operation only needs the
        encoded key and its one slot.  Keep the same integrity gates as the
        normal loader (both header copies, metadata, payload CRC, structural
        bounds and duplicate slot checks), but avoid decoding Python key
        values for unrelated rows.  ``False`` means that the side file is
        stale or malformed and the caller must use the authoritative full
        index path.  ``True, None``
        is a valid index proving that the key is absent.
        """

        key_columns = tuple(self.definition.primary_keys)
        if not key_columns or len(key) != len(key_columns):
            return False, None
        try:
            fields = tuple(self._fields_by_name[column] for column in key_columns)
            target_blob = b"".join(
                self._encode_field(value, field)
                for value, field in zip(key, fields)
            )
        except (KeyError, TypeError, ValueError, StorageError):
            return False, None

        # The fixed-width primary sidecar is the cold-start fast path.  It
        # carries the same generation/layout/CRC gates as the normal index;
        # a missing, stale or damaged sidecar simply falls through to the
        # existing authoritative equality-index probe below.
        direct_valid, direct_slot = self._load_primary_slot_index(key, int(generation))
        if direct_valid:
            return True, direct_slot

        # When several point reads share one observed generation, the probe
        # map is already integrity-checked.  Reuse it directly and avoid even
        # reopening the side-index file; a changed table generation falls
        # through to the header/CRC validation below.
        with self._cache_guard:
            cached_probe = self._primary_probe_cache
        if cached_probe is not None and cached_probe[0] == int(generation):
            target_slots = cached_probe[3].get(target_blob)
            if target_slots is None:
                return True, None
            if len(target_slots) != 1:
                return False, None
            return True, int(target_slots[0])

        path = self._index_path(key_columns)
        try:
            with path.open("rb") as handle:
                header_area = handle.read(_FIXED_INDEX_HEADER_SLOT * 2)
                if len(header_area) < _FIXED_INDEX_HEADER_SLOT * 2:
                    return False, None
                candidates = [
                    self._decode_index_header_slot(
                        header_area[offset : offset + _FIXED_INDEX_HEADER_SLOT]
                    )
                    for offset in (0, _FIXED_INDEX_HEADER_SLOT)
                ]
                valid = [candidate for candidate in candidates if candidate is not None]
                if not valid:
                    return False, None
                header = max(valid, key=lambda item: item["generation"])
                if (
                    header["generation"] != int(generation)
                    or header["layout_crc"] != self.layout_crc
                    or header["record_size"] != self.record_size
                    or header["max_records"] != self.max_records
                    or header["key_count"] != len(key_columns)
                ):
                    return False, None
                metadata = handle.read(header["metadata_length"])
                if metadata != self._index_metadata(key_columns):
                    return False, None
                payload_crc = int(header["payload_crc"])
                payload_length = int(header["payload_length"])
                with self._cache_guard:
                    cached_probe = self._primary_probe_cache
                if (
                    cached_probe is not None
                    and cached_probe[1] == payload_crc
                    and cached_probe[2] == payload_length
                ):
                    target_slots = cached_probe[3].get(target_blob)
                    if target_slots is None:
                        return True, None
                    if len(target_slots) != 1:
                        return False, None
                    return True, int(target_slots[0])
                payload = handle.read(payload_length)
                if len(payload) != payload_length:
                    return False, None
                if zlib.crc32(payload) & 0xFFFFFFFF != payload_crc:
                    return False, None
        except (OSError, StorageError, ValueError, TypeError):
            return False, None

        try:
            if len(payload) < _FIXED_INDEX_PAYLOAD_HEADER.size:
                return False, None
            payload_magic, count = _FIXED_INDEX_PAYLOAD_HEADER.unpack(
                payload[: _FIXED_INDEX_PAYLOAD_HEADER.size]
            )
            if payload_magic != _FIXED_INDEX_PAYLOAD_MAGIC or count > self.max_records:
                return False, None
            expected_key_length = sum(field.width for field in fields)
            cursor = _FIXED_INDEX_PAYLOAD_HEADER.size
            probe_map: dict[bytes, tuple[int, ...]] = {}
            seen_slots: set[int] = set()
            for _ in range(count):
                if cursor + _FIXED_INDEX_ENTRY_HEADER.size > len(payload):
                    return False, None
                key_length, position_count = _FIXED_INDEX_ENTRY_HEADER.unpack(
                    payload[cursor : cursor + _FIXED_INDEX_ENTRY_HEADER.size]
                )
                cursor += _FIXED_INDEX_ENTRY_HEADER.size
                if key_length != expected_key_length or position_count > self.max_records:
                    return False, None
                # A declared primary key must remain unique in the
                # acceleration file.  If an old/corrupt side file violates
                # that invariant, let the authoritative full decoder handle
                # the case instead of selecting an arbitrary slot.
                if position_count != 1:
                    return False, None
                key_end = cursor + key_length
                positions_end = key_end + position_count * _FIXED_INDEX_SLOT.size
                if key_end > len(payload) or positions_end > len(payload):
                    return False, None
                key_blob = payload[cursor:key_end]
                positions: list[int] = []
                for position in range(position_count):
                    slot = _FIXED_INDEX_SLOT.unpack(
                        payload[
                            key_end + position * _FIXED_INDEX_SLOT.size :
                            key_end + (position + 1) * _FIXED_INDEX_SLOT.size
                        ]
                    )[0]
                    if slot >= self.max_records or slot in seen_slots:
                        return False, None
                    seen_slots.add(int(slot))
                    positions.append(int(slot))
                if key_blob in probe_map:
                    # A duplicate encoded key makes the supposedly unique
                    # primary index unusable; let the full decoder report it.
                    return False, None
                probe_map[bytes(key_blob)] = tuple(positions)
                cursor = positions_end
            # Replay compact add/remove frames using encoded keys.  This
            # retains the incremental side-index design without decoding
            # unrelated Python key objects, and keeps the probe valid after
            # multi-process appends/deletes.
            while cursor < len(payload):
                if cursor + _FIXED_INDEX_DELTA_HEADER.size > len(payload):
                    return False, None
                magic, operation, slot, key_length = _FIXED_INDEX_DELTA_HEADER.unpack(
                    payload[cursor : cursor + _FIXED_INDEX_DELTA_HEADER.size]
                )
                if magic != _FIXED_INDEX_DELTA_MAGIC or key_length != expected_key_length:
                    return False, None
                cursor += _FIXED_INDEX_DELTA_HEADER.size
                key_end = cursor + key_length
                if key_end > len(payload) or slot >= self.max_records:
                    return False, None
                key_blob = bytes(payload[cursor:key_end])
                positions = probe_map.get(key_blob, ())
                if operation == _FIXED_INDEX_DELTA_ADD:
                    if positions or slot in seen_slots:
                        return False, None
                    probe_map[key_blob] = (int(slot),)
                    seen_slots.add(int(slot))
                elif operation == _FIXED_INDEX_DELTA_REMOVE:
                    if len(positions) != 1 or int(slot) != positions[0]:
                        return False, None
                    probe_map.pop(key_blob, None)
                    seen_slots.discard(int(slot))
                else:
                    return False, None
                cursor = key_end
            if cursor != len(payload):
                return False, None
            with self._cache_guard:
                self._primary_probe_cache = (
                    int(generation),
                    payload_crc,
                    payload_length,
                    probe_map,
                )
            target_slots = probe_map.get(target_blob)
            if target_slots is None:
                return True, None
            if len(target_slots) != 1:
                return False, None
            return True, target_slots[0]
        except (IndexError, struct.error, ValueError, TypeError):
            return False, None

    def _persisted_index_descriptors(
        self,
        generation: int,
        *,
        check_directory: bool = True,
    ) -> tuple[tuple[tuple[str, ...], Path, dict[str, int]], ...]:
        """List current-generation side indexes without decoding their maps."""

        # A value-only update calls this method on every commit.  The side
        # files are immutable for a given table generation, so avoid repeating
        # the directory enumeration and metadata decoding until the table
        # generation or directory contents changes.
        with self._cache_guard:
            cached = self._index_descriptor_cache
        # Value-only commits already hold the table's inter-process lock, so
        # no other writer can replace a sidecar during this operation.  Reuse
        # the previously validated descriptor cache without another parent
        # directory ``stat``; the sidecar remains disposable and a later
        # structural operation performs the full replacement check.  If there
        # is no descriptor cache yet, fall back to the normal discovery path
        # once so the first update still validates the file identity.
        if not check_directory and cached is not None:
            return tuple(
                descriptor
                for descriptor in cached[1]
                if descriptor[2]["generation"] == int(generation)
            )
        directory_signature = _file_signature(self.path.parent)
        if cached is not None and cached[0] != directory_signature:
            # A directory change can mean that another process compacted or
            # replaced an index file.  Release cached handles before loading
            # the new descriptors; in-place header updates do not change the
            # directory and retain the fast path.
            self._close_index_handles()
        if cached is not None and cached[0] == directory_signature:
            cached_descriptors = cached[1]
            # The table lock serialises local writers, and local in-place
            # updates publish their new header through
            # ``_remember_index_descriptor`` below.  If another process
            # advances/replaces an index, the requested table generation no
            # longer matches this process's descriptor and the side index is
            # conservatively skipped; the authoritative table path will
            # rebuild it on demand.  Avoiding a stat for every descriptor on
            # every commit keeps this cache cheaper than reopening the file.
            return tuple(
                descriptor
                for descriptor in cached_descriptors
                if descriptor[2]["generation"] == int(generation)
            )

        descriptors: list[tuple[tuple[str, ...], Path, dict[str, int]]] = []
        try:
            candidates = tuple(self.path.parent.glob(f"{self.path.name}.fixedidx.*.bin"))
        except OSError:
            result: tuple[tuple[tuple[str, ...], Path, dict[str, int]], ...] = ()
            with self._cache_guard:
                self._index_descriptor_cache = (directory_signature, result)
            return result
        for path in candidates:
            try:
                with path.open("rb") as handle:
                    header_area = handle.read(_FIXED_INDEX_HEADER_SLOT * 2)
                    valid = [
                        self._decode_index_header_slot(
                            header_area[offset : offset + _FIXED_INDEX_HEADER_SLOT]
                        )
                        for offset in (0, _FIXED_INDEX_HEADER_SLOT)
                        if len(header_area[offset : offset + _FIXED_INDEX_HEADER_SLOT]) == _FIXED_INDEX_HEADER_SLOT
                    ]
                    valid = [item for item in valid if item is not None]
                    if not valid:
                        continue
                    header = max(valid, key=lambda item: item["generation"])
                    if (
                        header["generation"] != int(generation)
                        or header["layout_crc"] != self.layout_crc
                        or header["record_size"] != self.record_size
                        or header["max_records"] != self.max_records
                    ):
                        continue
                    metadata = handle.read(header["metadata_length"])
                    cursor = 0
                    key_columns: list[str] = []
                    for _ in range(header["key_count"]):
                        if cursor + 2 > len(metadata):
                            raise ValueError("invalid index metadata")
                        length = struct.unpack(">H", metadata[cursor : cursor + 2])[0]
                        cursor += 2
                        end = cursor + length
                        if end > len(metadata):
                            raise ValueError("invalid index metadata")
                        key_columns.append(metadata[cursor:end].decode("utf-8"))
                        cursor = end
                    columns = tuple(key_columns)
                    if cursor != len(metadata) or metadata != self._index_metadata(columns):
                        continue
                    descriptors.append((columns, path, header))
            except (OSError, ValueError, UnicodeError, StorageError, TypeError):
                continue
        result = tuple(descriptors)
        with self._cache_guard:
            self._index_descriptor_cache = (directory_signature, result)
        return tuple(
            descriptor for descriptor in result if descriptor[2]["generation"] == int(generation)
        )

    def _remember_index_descriptor(
        self,
        path: Path,
        header: Mapping[str, int],
    ) -> None:
        """Publish an in-place side-index header to the local descriptor cache."""

        with self._cache_guard:
            cached = self._index_descriptor_cache
            if cached is None:
                return
            directory_signature, descriptors = cached
            updated = []
            found = False
            for columns, descriptor_path, descriptor_header in descriptors:
                if descriptor_path == path:
                    updated.append((columns, descriptor_path, dict(header)))
                    found = True
                else:
                    updated.append((columns, descriptor_path, descriptor_header))
            if found:
                self._index_descriptor_cache = (
                    directory_signature,
                    tuple(updated),
                )

    def _persist_key_index(
        self,
        key_columns: Sequence[str],
        generation: int,
        index: Mapping[tuple[Any, ...], Sequence[int]],
    ) -> None:
        """Atomically publish an acceleration-only equality index side file."""

        try:
            metadata, payload = self._encode_persisted_index(key_columns, index)
            body = _FIXED_INDEX_HEADER_BODY.pack(
                _FIXED_INDEX_MAGIC,
                _FIXED_INDEX_VERSION,
                int(generation),
                self.layout_crc,
                self.record_size,
                self.max_records,
                len(tuple(key_columns)),
                len(metadata),
                len(payload),
                zlib.crc32(payload) & 0xFFFFFFFF,
            )
            slot = self._index_header_slot(body)
            path = self._index_path(key_columns)
            temporary: Optional[str] = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w+b", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
                ) as handle:
                    temporary = handle.name
                    handle.write(slot)
                    handle.write(slot)
                    handle.write(metadata)
                    handle.write(payload)
                    handle.flush()
                    # Side indexes are not part of commit durability, but a
                    # fully written temporary file prevents readers from ever
                    # observing a partial payload after os.replace().
                    os.fsync(handle.fileno())
                # A cached writable handle refers to the old inode on
                # platforms that do not permit replacing an open file.
                self._invalidate_index_handle(path)
                with self._cache_guard:
                    self._index_descriptor_cache = None
                os.replace(temporary, path)
                temporary = None
            finally:
                if temporary:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
            if tuple(key_columns) == tuple(self.definition.primary_keys):
                self._persist_primary_slot_index(int(generation), index)
        except (OSError, StorageError, SchemaError, ValueError, TypeError):
            # A side index is disposable.  A disk-full or concurrent cleanup
            # must never turn a successful table commit into a failed commit.
            return

    def _append_primary_slot_deltas(
        self,
        previous_generation: int,
        generation: int,
        additions: Mapping[tuple[Any, ...], int],
        removals: Mapping[tuple[Any, ...], set[int]],
    ) -> None:
        """Append primary-key sidecar deltas without rewriting its base map."""

        if not additions and not removals:
            return
        key_columns = tuple(self.definition.primary_keys)
        if not key_columns:
            return
        # A one-row append/delete is already protected by the authoritative
        # table WAL and is usually followed by another write under the same
        # process lock.  Defer this disposable sidecar maintenance for that
        # tiny case; the generation mismatch makes readers safely fall back to
        # the regular equality index, while multi-row batches still get the
        # direct point-lookup benefit.
        change_count = len(additions) + sum(len(slots) for slots in removals.values())
        if change_count == 1:
            return
        try:
            fields = tuple(self._fields_by_name[column] for column in key_columns)
            frames = bytearray()
            for key, slots in removals.items():
                key_blob = b"".join(
                    self._encode_field(value, field)
                    for value, field in zip(key, fields)
                )
                for slot in slots:
                    frames.extend(
                        _FIXED_PRIMARY_DELTA_HEADER.pack(
                            _FIXED_PRIMARY_DELTA_MAGIC,
                            _FIXED_PRIMARY_DELTA_REMOVE,
                            int(slot),
                            len(key_blob),
                        )
                    )
                    frames.extend(key_blob)
            for key, slot in additions.items():
                key_blob = b"".join(
                    self._encode_field(value, field)
                    for value, field in zip(key, fields)
                )
                frames.extend(
                    _FIXED_PRIMARY_DELTA_HEADER.pack(
                        _FIXED_PRIMARY_DELTA_MAGIC,
                        _FIXED_PRIMARY_DELTA_ADD,
                        int(slot),
                        len(key_blob),
                    )
                )
                frames.extend(key_blob)
            if not frames:
                return
            path = self._primary_slot_path()
            compact = False
            handle = self._index_update_handle(path)
            try:
                header_area = handle.read(_FIXED_PRIMARY_HEADER_SLOT * 2)
                valid = [
                    self._decode_primary_header_slot(
                        header_area[offset : offset + _FIXED_PRIMARY_HEADER_SLOT]
                    )
                    for offset in (0, _FIXED_PRIMARY_HEADER_SLOT)
                ]
                valid = [item for item in valid if item is not None]
                if not valid:
                    return
                header = max(valid, key=lambda item: item["generation"])
                if (
                    header["generation"] != int(previous_generation)
                    or header["layout_crc"] != self.layout_crc
                    or header["record_size"] != self.record_size
                    or header["max_records"] != self.max_records
                    or header["key_width"] != sum(field.width for field in fields)
                ):
                    return
                handle.seek(0, os.SEEK_END)
                handle.write(frames)
                new_length = int(handle.tell())
                handle.flush()
                body = _FIXED_PRIMARY_HEADER_BODY.pack(
                    _FIXED_PRIMARY_MAGIC,
                    _FIXED_PRIMARY_VERSION,
                    int(generation),
                    header["layout_crc"],
                    header["record_size"],
                    header["max_records"],
                    header["key_width"],
                    header["count"],
                    zlib.crc32(frames, header["payload_crc"]) & 0xFFFFFFFF,
                )
                handle.seek((int(generation) & 1) * _FIXED_PRIMARY_HEADER_SLOT)
                handle.write(self._primary_header_slot(body))
                handle.flush()
                base_payload_length = _FIXED_PRIMARY_PAYLOAD_HEADER.size + header["count"] * (
                    header["key_width"] + _FIXED_PRIMARY_ENTRY_TAIL.size
                )
                compact = (
                    new_length
                    - _FIXED_PRIMARY_HEADER_SLOT * 2
                    - base_payload_length
                    >= _FIXED_PRIMARY_DELTA_COMPACT_BYTES
                )
            finally:
                self._remember_index_handle(path, handle)
            if compact:
                # Rebuild only the disposable probe sidecar from the already
                # validated equality index.  This bounds delta replay time;
                # normal table/WAL durability is unaffected if compaction is
                # interrupted or the side file is unavailable.
                rebuilt = self._load_persisted_key_index(key_columns, int(generation))
                if rebuilt is not None:
                    self._persist_primary_slot_index(int(generation), rebuilt)
        except (OSError, StorageError, SchemaError, ValueError, TypeError, UnicodeError):
            return

    def _append_persisted_index_deltas(
        self,
        previous_generation: int,
        generation: int,
        additions_by_columns: Optional[Mapping[tuple[str, ...], Any]],
        removal_keys_by_cache: Mapping[tuple[str, ...], Mapping[tuple[Any, ...], set[int]]],
        entries: Sequence[tuple[int, bytes]],
        operation: Optional[str],
    ) -> set[tuple[str, ...]]:
        """Append structural index changes without rewriting the base map.

        The fixed table commit has already reached its durable generation when
        this method runs.  Each side file receives a sequence of add/remove
        frames and then one CRC-checked header update.  A torn side update is
        therefore ignored by the loader, while normal appends/deletes touch
        only a few dozen bytes.  The returned keys have exceeded the delta
        compaction threshold and may be rewritten from the in-memory map once
        the commit cache has been updated.
        """

        if not additions_by_columns and not removal_keys_by_cache:
            return set()
        additions = {
            tuple(columns): list(values.items() if isinstance(values, Mapping) else values)
            for columns, values in (additions_by_columns or {}).items()
            if values
        }
        descriptors = self._persisted_index_descriptors(previous_generation)
        descriptor_columns = {columns for columns, _path, _header in descriptors}
        if operation == "add":
            # A writer may not have loaded a side index in its own process.
            # Derive keys directly from the encoded insert records so that an
            # existing side file still receives a small delta frame.
            for key_columns in descriptor_columns:
                if key_columns in additions:
                    continue
                try:
                    fields = tuple(self._fields_by_name[column] for column in key_columns)
                    generated: list[tuple[tuple[Any, ...], int]] = []
                    for slot, record in entries:
                        state, _order, checksum = _FIXED_SLOT_HEADER.unpack(record[:_FIXED_SLOT_HEADER.size])
                        if state != 1:
                            continue
                        payload = record[_FIXED_SLOT_HEADER.size :]
                        if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                            continue
                        values = tuple(
                            self._decode_field(payload[field.offset : field.offset + field.width], field)
                            for field in fields
                        )
                        hash(values)
                        generated.append((values, int(slot)))
                    if generated:
                        additions[key_columns] = generated
                except (KeyError, StorageError, TypeError, ValueError, struct.error):
                    continue
        keys_to_update = set(additions).union(removal_keys_by_cache)
        compact: set[tuple[str, ...]] = set()
        for key_columns in keys_to_update:
            add_values = additions.get(key_columns, ())
            remove_values = removal_keys_by_cache.get(key_columns, {})
            try:
                fields = tuple(self._fields_by_name[column] for column in key_columns)
                frames = bytearray()
                for removed_key, removed_slots in remove_values.items():
                    key_blob = b"".join(
                        self._encode_field(value, field)
                        for value, field in zip(removed_key, fields)
                    )
                    if len(key_blob) > 0xFFFFFFFF:
                        continue
                    for slot in removed_slots:
                        frames.extend(
                            _FIXED_INDEX_DELTA_HEADER.pack(
                                _FIXED_INDEX_DELTA_MAGIC,
                                _FIXED_INDEX_DELTA_REMOVE,
                                int(slot),
                                len(key_blob),
                            )
                        )
                        frames.extend(key_blob)
                for added_key, slot in add_values:
                    key_blob = b"".join(
                        self._encode_field(value, field)
                        for value, field in zip(added_key, fields)
                    )
                    if len(key_blob) > 0xFFFFFFFF:
                        continue
                    frames.extend(
                        _FIXED_INDEX_DELTA_HEADER.pack(
                            _FIXED_INDEX_DELTA_MAGIC,
                            _FIXED_INDEX_DELTA_ADD,
                            int(slot),
                            len(key_blob),
                        )
                    )
                    frames.extend(key_blob)
                if not frames:
                    continue
                path = self._index_path(key_columns)
                handle = self._index_update_handle(path)
                try:
                    handle.seek(0)
                    header_area = handle.read(_FIXED_INDEX_HEADER_SLOT * 2)
                    if len(header_area) < _FIXED_INDEX_HEADER_SLOT * 2:
                        continue
                    candidates = [
                        self._decode_index_header_slot(
                            header_area[offset : offset + _FIXED_INDEX_HEADER_SLOT]
                        )
                        for offset in (0, _FIXED_INDEX_HEADER_SLOT)
                    ]
                    valid = [candidate for candidate in candidates if candidate is not None]
                    if not valid:
                        continue
                    header = max(valid, key=lambda item: item["generation"])
                    if (
                        header["generation"] != int(previous_generation)
                        or header["layout_crc"] != self.layout_crc
                        or header["record_size"] != self.record_size
                        or header["max_records"] != self.max_records
                        or header["key_count"] != len(key_columns)
                    ):
                        continue
                    metadata = handle.read(header["metadata_length"])
                    if metadata != self._index_metadata(key_columns):
                        continue
                    payload_offset = _FIXED_INDEX_HEADER_SLOT * 2 + header["metadata_length"]
                    end = payload_offset + header["payload_length"]
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() < end:
                        continue
                    # An interrupted previous append may have left bytes past
                    # the generation currently advertised by the header.
                    # Overwrite them so the new CRC covers exactly one stream.
                    handle.seek(end)
                    handle.write(frames)
                    new_length = header["payload_length"] + len(frames)
                    if new_length > _FIXED_INDEX_MAX_PAYLOAD:
                        continue
                    handle.truncate(payload_offset + new_length)
                    handle.flush()
                    body = _FIXED_INDEX_HEADER_BODY.pack(
                        _FIXED_INDEX_MAGIC,
                        _FIXED_INDEX_VERSION,
                        int(generation),
                        header["layout_crc"],
                        header["record_size"],
                        header["max_records"],
                        header["key_count"],
                        header["metadata_length"],
                        new_length,
                        zlib.crc32(frames, header["payload_crc"]) & 0xFFFFFFFF,
                    )
                    slot = self._index_header_slot(body)
                    handle.seek((int(generation) & 1) * _FIXED_INDEX_HEADER_SLOT)
                    handle.write(slot)
                    handle.flush()
                    updated_header = dict(header)
                    updated_header["generation"] = int(generation)
                    updated_header["payload_length"] = int(new_length)
                    updated_header["payload_crc"] = zlib.crc32(frames, header["payload_crc"]) & 0xFFFFFFFF
                    self._remember_index_descriptor(path, updated_header)
                    if new_length >= _FIXED_INDEX_DELTA_COMPACT_BYTES:
                        compact.add(key_columns)
                finally:
                    self._remember_index_handle(path, handle)
            except (OSError, StorageError, SchemaError, ValueError, TypeError, UnicodeError):
                continue
        # Keep the fixed-width primary sidecar in the same append-only spirit
        # as the equality index.  If the regular descriptor was unavailable,
        # derive just the primary key values from the encoded insert records.
        primary_columns = tuple(self.definition.primary_keys)
        primary_additions = dict(additions.get(primary_columns, {}))
        if operation == "add" and not primary_additions:
            try:
                fields = tuple(self._fields_by_name[column] for column in primary_columns)
                for slot, record in entries:
                    state, _order, checksum = _FIXED_SLOT_HEADER.unpack(record[:_FIXED_SLOT_HEADER.size])
                    if state != 1:
                        continue
                    payload = record[_FIXED_SLOT_HEADER.size :]
                    if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                        continue
                    key = tuple(
                        self._decode_field(payload[field.offset : field.offset + field.width], field)
                        for field in fields
                    )
                    hash(key)
                    primary_additions[key] = int(slot)
            except (KeyError, StorageError, TypeError, ValueError, struct.error):
                primary_additions = {}
        self._append_primary_slot_deltas(
            int(previous_generation),
            int(generation),
            primary_additions,
            removal_keys_by_cache.get(primary_columns, {}),
        )
        return compact

    def _advance_persisted_indexes(
        self,
        previous_generation: int,
        generation: int,
        changed_columns: Optional[set[str]],
    ) -> None:
        """Advance unaffected side-index headers after a value-only commit.

        Structural changes (insert/delete/replace) pass ``None`` and leave all
        side files stale.  A later access rebuilds them safely.  For an update
        that only changes row values, side indexes whose key columns are not
        touched keep the same slot map; only their redundant header generation
        is advanced, so a new process can reuse them without a table scan.
        """

        if changed_columns is None or int(generation) != int(previous_generation) + 1:
            return
        # A value-only commit does not need to publish every sidecar header.
        # An equality sidecar is an optional cache, and an untouched index
        # that has never been used by this process can safely remain at the
        # previous generation.  The next cold reader will reject that stale
        # generation and rebuild from the authoritative mmap/WAL.  Keeping
        # only indexes already used by a caller avoids opening and rewriting
        # unrelated sidecar files on every short update while still
        # preserving the fast reopen path for indexes the process actually
        # uses.
        with self._cache_guard:
            active_columns = set(self._sidecar_active_columns)
        if not active_columns:
            return
        # Reuse the validated descriptors from the current generation.  This
        # preserves the same generation/layout checks while avoiding a second
        # glob and a second header/metadata decode in the common update path.
        for key_columns, path, header in self._persisted_index_descriptors(
            previous_generation,
            check_directory=False,
        ):
            try:
                if key_columns not in active_columns:
                    continue
                handle = self._index_update_handle(path)
                try:
                    if set(key_columns).intersection(changed_columns):
                        continue
                    body = _FIXED_INDEX_HEADER_BODY.pack(
                        _FIXED_INDEX_MAGIC,
                        _FIXED_INDEX_VERSION,
                        int(generation),
                        header["layout_crc"],
                        header["record_size"],
                        header["max_records"],
                        header["key_count"],
                        header["metadata_length"],
                        header["payload_length"],
                        header["payload_crc"],
                    )
                    slot = self._index_header_slot(body)
                    handle.seek((int(generation) & 1) * _FIXED_INDEX_HEADER_SLOT)
                    handle.write(slot)
                    handle.flush()
                    updated_header = dict(header)
                    updated_header["generation"] = int(generation)
                    self._remember_index_descriptor(path, updated_header)
                finally:
                    self._remember_index_handle(path, handle)
            except (OSError, ValueError, UnicodeError, TypeError, StorageError):
                continue
        # A value-only commit leaves encoded primary keys and slots unchanged,
        # so advance the independent fixed-width probe header in place too.
        # It is intentionally not fsynced: the table WAL is the durability
        # source and a torn disposable sidecar is rejected on the next read.
        primary_columns = tuple(self.definition.primary_keys)
        if primary_columns and primary_columns in active_columns and not set(primary_columns).intersection(changed_columns):
            self._advance_primary_slot_generation(int(previous_generation), int(generation))

    def _advance_primary_slot_generation(self, previous_generation: int, generation: int) -> None:
        path = self._primary_slot_path()
        try:
            # Reuse the same unbuffered sidecar handle as structural delta
            # updates.  Opening and closing this tiny file for every value
            # update was visible in short single-row transactions on Windows;
            # the table lock and descriptor-generation checks already fence
            # replacement, while the sidecar itself remains disposable.
            handle = self._index_update_handle(path)
            try:
                handle.seek(0)
                header_area = handle.read(_FIXED_PRIMARY_HEADER_SLOT * 2)
                valid = [
                    self._decode_primary_header_slot(
                        header_area[offset : offset + _FIXED_PRIMARY_HEADER_SLOT]
                    )
                    for offset in (0, _FIXED_PRIMARY_HEADER_SLOT)
                ]
                valid = [item for item in valid if item is not None]
                if not valid:
                    return
                header = max(valid, key=lambda item: item["generation"])
                if (
                    header["generation"] != int(previous_generation)
                    or header["layout_crc"] != self.layout_crc
                    or header["record_size"] != self.record_size
                    or header["max_records"] != self.max_records
                ):
                    return
                body = _FIXED_PRIMARY_HEADER_BODY.pack(
                    _FIXED_PRIMARY_MAGIC,
                    _FIXED_PRIMARY_VERSION,
                    int(generation),
                    header["layout_crc"],
                    header["record_size"],
                    header["max_records"],
                    header["key_width"],
                    header["count"],
                    header["payload_crc"],
                )
                handle.seek((int(generation) & 1) * _FIXED_PRIMARY_HEADER_SLOT)
                handle.write(self._primary_header_slot(body))
                handle.flush()
            finally:
                self._remember_index_handle(path, handle)
        except (OSError, ValueError, TypeError, StorageError):
            return

    @contextmanager
    def _mapped(self, *, write: bool = False) -> Iterator[tuple[Any, mmap.mmap]]:
        # Keep the mapping alive for the entire decode/write window.  Readers
        # share the permit; local publishers can request ``write=True`` to
        # exclude them while changing slots.  Cross-process publication is
        # still ordered by the durable WAL and generation header.
        if write:
            with self._mapping_guard:
                yield self._ensure_mapping()
        else:
            with self._mapping_guard.read():
                yield self._ensure_mapping()

    def _slot_offset(self, slot: int) -> int:
        if slot < 0 or slot >= self.max_records:
            raise StorageError(f"fixed table slot out of range: {slot}")
        return _FIXED_DATA_OFFSET + slot * self.record_size

    def _order_for_generation(self, generation: int, position: int = 0) -> int:
        """Return a monotonic insertion order without scanning all slots."""

        stride = self.max_records + 1
        order = int(generation) * stride + int(position)
        if order > 0xFFFFFFFFFFFFFFFF:
            raise StorageError(f"fixed table insertion order exhausted: {self.path}")
        return order

    def _encode_field(self, value: Any, field: _FixedField) -> bytes:
        payload_width = field.width - 1
        if value is None:
            return bytes(field.width)
        if field.kind == "int":
            payload = struct.pack(">q", int(value))
        elif field.kind == "float":
            payload = struct.pack(">d", float(value))
        elif field.kind == "bool":
            payload = bytes((1 if value else 0,))
        elif field.kind == "str":
            payload = str(value).encode("utf-8")
        else:
            try:
                payload = pickle.dumps(value, protocol=5)
            except (TypeError, ValueError, pickle.PicklingError) as exc:
                raise StorageError(f"cannot encode column {field.name!r}: {exc}") from exc
            payload = struct.pack(">I", len(payload)) + payload
        if len(payload) > payload_width:
            raise SchemaError(f"column {field.name!r} exceeds fixed storage width {payload_width}")
        return b"\1" + payload + bytes(payload_width - len(payload))

    def _decode_field(self, raw: Any, field: _FixedField) -> Any:
        if not raw or raw[0] == 0:
            return None
        payload = raw[1:]
        if field.kind == "int":
            return struct.unpack(">q", payload[:8])[0]
        if field.kind == "float":
            return struct.unpack(">d", payload[:8])[0]
        if field.kind == "bool":
            return bool(payload[0])
        if field.kind == "str":
            return bytes(payload).rstrip(b"\0").decode("utf-8")
        if len(payload) < 4:
            raise StorageError(f"invalid fixed object field {field.name!r}: {self.path}")
        length = struct.unpack(">I", payload[:4])[0]
        if length > len(payload) - 4:
            raise StorageError(f"invalid fixed object length {field.name!r}: {self.path}")
        try:
            return pickle.loads(bytes(payload[4 : 4 + length]))
        except (EOFError, pickle.UnpicklingError, ValueError) as exc:
            raise StorageError(f"cannot decode fixed object field {field.name!r}: {self.path}") from exc

    def _encode_payload(self, row: Mapping[str, Any]) -> bytes:
        payload = bytearray(self.payload_size)
        for field in self.fields:
            encoded = self._encode_field(row.get(field.name), field)
            payload[field.offset : field.offset + field.width] = encoded
        return bytes(payload)

    def _decode_payload(self, payload: Any, *, fields: Optional[Sequence[_FixedField]] = None) -> dict[str, Any]:
        selected = self.fields if fields is None else fields
        result: dict[str, Any] = {}
        for field in selected:
            result[field.name] = self._decode_field(payload[field.offset : field.offset + field.width], field)
        return result

    def _encode_record(self, row: Mapping[str, Any], order: int) -> bytes:
        payload = self._encode_payload(row)
        return _FIXED_SLOT_HEADER.pack(1, int(order), zlib.crc32(payload) & 0xFFFFFFFF) + payload

    def _decode_record(self, raw: Any, *, fields: Optional[Sequence[_FixedField]] = None) -> tuple[int, Optional[dict[str, Any]]]:
        if len(raw) != self.record_size:
            raise StorageError(f"invalid fixed record size: {self.path}")
        state, order, checksum = _FIXED_SLOT_HEADER.unpack(raw[:_FIXED_SLOT_HEADER.size])
        if state == 0:
            return order, None
        if state != 1:
            raise StorageError(f"invalid fixed record state: {self.path}")
        payload = raw[_FIXED_SLOT_HEADER.size:]
        if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
            raise StorageError(f"fixed record checksum mismatch: {self.path}")
        return order, self._decode_payload(payload, fields=fields)

    def _read_record(self, mapping: mmap.mmap, slot: int, *, fields: Optional[Sequence[_FixedField]] = None) -> tuple[int, Optional[dict[str, Any]]]:
        offset = self._slot_offset(slot)
        # mmap slicing creates a full bytes copy.  A short-lived memoryview
        # lets CRC validation and scalar decoding read the mapped bytes in
        # place; string/object fields are copied only when they are actually
        # materialised by _decode_field.
        raw = memoryview(mapping)[offset : offset + self.record_size]
        try:
            return self._decode_record(raw, fields=fields)
        finally:
            raw.release()

    def _read_index_entry(
        self,
        mapping: mmap.mmap,
        slot: int,
        fields: Sequence[_FixedField],
        *,
        offset: Optional[int] = None,
    ) -> tuple[int, Optional[tuple[Any, ...]]]:
        """Read only the fields needed to build one equality-index entry.

        Index construction used to call ``_read_record`` and allocate a
        temporary dictionary for every occupied slot, even though the caller
        immediately converted that dictionary back into a tuple.  Keep the
        same state and full-payload CRC validation, but decode just the
        indexed fields and return their tuple directly.
        """

        if offset is None:
            offset = self._slot_offset(slot)
        raw = memoryview(mapping)[offset : offset + self.record_size]
        try:
            if len(raw) != self.record_size:
                raise StorageError(f"invalid fixed record size: {self.path}")
            state, order, checksum = _FIXED_SLOT_HEADER.unpack(raw[:_FIXED_SLOT_HEADER.size])
            if state == 0:
                return order, None
            if state != 1:
                raise StorageError(f"invalid fixed record state: {self.path}")
            payload = raw[_FIXED_SLOT_HEADER.size :]
            if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                raise StorageError(f"fixed record checksum mismatch: {self.path}")
            return order, tuple(
                self._decode_field(payload[field.offset : field.offset + field.width], field)
                for field in fields
            )
        finally:
            raw.release()

    def _wal_frame(self, generation: int, row_count: int, entries: Sequence[tuple[int, bytes]]) -> bytes:
        payload = bytearray(_FIXED_WAL_PAYLOAD_HEADER.pack(generation, row_count, len(entries)))
        for slot, record in entries:
            if len(record) != self.record_size:
                raise StorageError(f"invalid fixed WAL record size: {self.path}")
            payload.extend(_FIXED_WAL_ENTRY_HEADER.pack(slot, len(record)))
            payload.extend(record)
        if len(payload) > _MAX_WAL_RECORD:
            raise StorageError(
                f"fixed WAL frame is too large ({len(payload)} bytes > {_MAX_WAL_RECORD}): {self.path}"
            )
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        return _FIXED_WAL_HEADER.pack(_FIXED_WAL_MAGIC, len(payload), checksum) + payload + _FIXED_WAL_TRAILER.pack(_FIXED_WAL_END_MAGIC, len(payload))

    def _append_wal_locked(self, generation: int, row_count: int, entries: Sequence[tuple[int, bytes]]) -> int:
        frame = self._wal_frame(generation, row_count, entries)
        try:
            handle = self._ensure_wal_handle()
            with self._wal_probe_guard:
                base_position = self._wal_position
                if base_position is None:
                    handle.seek(0, os.SEEK_END)
                    base_position = int(handle.tell())
                handle.write(frame)
                handle.flush()
                os.fsync(handle.fileno())
                self._wal_position = int(base_position) + len(frame)
                self._wal_observed_size = int(self._wal_position)
                self._wal_observed_tail_generation = int(generation)
        except OSError as exc:
            raise StorageError(f"cannot append fixed WAL {self.wal_path}: {exc}") from exc
        # Writers hold the inter-process table lock, so no other writer can
        # append between this durable write and the publish/cleanup steps.
        # Returning the frame size lets the commit path avoid a second stat()
        # solely to decide whether the compaction threshold was crossed.
        return len(frame)

    def _read_wal(self) -> tuple[list[tuple[int, int, list[tuple[int, bytes]]]], int, bool]:
        if self._wal_size() == 0:
            return [], 0, False
        records: list[tuple[int, int, list[tuple[int, bytes]]]] = []
        valid_end = 0
        incomplete = False
        handle = self._wal_handle
        owned = False
        self._wal_probe_guard.acquire()
        try:
            if handle is None:
                handle = self.wal_path.open("rb")
                owned = True
            handle.seek(0)
            while True:
                    header = handle.read(_FIXED_WAL_HEADER.size)
                    if not header:
                        break
                    if len(header) < _FIXED_WAL_HEADER.size:
                        incomplete = True
                        break
                    magic, size, checksum = _FIXED_WAL_HEADER.unpack(header)
                    if magic != _FIXED_WAL_MAGIC or size > _MAX_WAL_RECORD:
                        raise StorageError(f"invalid fixed WAL header: {self.wal_path}")
                    payload = handle.read(size)
                    if len(payload) < size:
                        incomplete = True
                        break
                    trailer = handle.read(_FIXED_WAL_TRAILER.size)
                    if len(trailer) < _FIXED_WAL_TRAILER.size:
                        incomplete = True
                        break
                    end_magic, end_size = _FIXED_WAL_TRAILER.unpack(trailer)
                    if end_magic != _FIXED_WAL_END_MAGIC or end_size != size:
                        raise StorageError(f"invalid fixed WAL trailer: {self.wal_path}")
                    if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                        raise StorageError(f"fixed WAL checksum mismatch: {self.wal_path}")
                    if len(payload) < _FIXED_WAL_PAYLOAD_HEADER.size:
                        raise StorageError(f"invalid fixed WAL payload: {self.wal_path}")
                    generation, row_count, count = _FIXED_WAL_PAYLOAD_HEADER.unpack(payload[:_FIXED_WAL_PAYLOAD_HEADER.size])
                    cursor = _FIXED_WAL_PAYLOAD_HEADER.size
                    entries: list[tuple[int, bytes]] = []
                    for _ in range(count):
                        if cursor + _FIXED_WAL_ENTRY_HEADER.size > len(payload):
                            raise StorageError(f"invalid fixed WAL entry: {self.wal_path}")
                        slot, record_size = _FIXED_WAL_ENTRY_HEADER.unpack(payload[cursor : cursor + _FIXED_WAL_ENTRY_HEADER.size])
                        cursor += _FIXED_WAL_ENTRY_HEADER.size
                        if record_size != self.record_size or cursor + record_size > len(payload):
                            raise StorageError(f"invalid fixed WAL record: {self.wal_path}")
                        entries.append((slot, bytes(payload[cursor : cursor + record_size])))
                        cursor += record_size
                    if cursor != len(payload):
                        raise StorageError(f"invalid fixed WAL payload length: {self.wal_path}")
                    records.append((generation, row_count, entries))
                    valid_end += _FIXED_WAL_HEADER.size + size + _FIXED_WAL_TRAILER.size
        except OSError as exc:
            raise StorageError(f"cannot read fixed WAL {self.wal_path}: {exc}") from exc
        finally:
            if owned and handle is not None:
                handle.close()
            elif handle is self._wal_handle:
                # Reading seeks the shared handle to the beginning.  Do not
                # trust the previous append offset after that operation;
                # re-seek once before the next append.
                self._wal_position = None
            self._wal_probe_guard.release()
        return records, valid_end, incomplete

    def _truncate_wal_locked(
        self,
        length: int = 0,
        *,
        sync: bool = True,
        known_nonempty: bool = False,
    ) -> None:
        if not known_nonempty and self._wal_size() == 0 and length == 0:
            return
        self._wal_probe_guard.acquire()
        try:
            handle = self._wal_handle
            owned = False
            if handle is None:
                handle = _open_fixed_mapping_file(self.wal_path)
                owned = True
            handle.seek(0)
            handle.truncate(length)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
            if owned:
                handle.close()
            else:
                self._wal_position = int(length)
            self._wal_observed_size = int(length)
            self._wal_observed_tail_generation = None
        except OSError as exc:
            raise StorageError(f"cannot truncate fixed WAL {self.wal_path}: {exc}") from exc
        finally:
            self._wal_probe_guard.release()

    def _write_header_locked(self, generation: int, row_count: int) -> None:
        slot = self._header_slot(self._header_body(generation, row_count))
        offset = (generation & 1) * _FIXED_HEADER_SLOT
        try:
            handle, mapping = self._ensure_mapping()
            mapping[offset : offset + len(slot)] = slot
            _flush_mapped_ranges(mapping, [(offset, len(slot))])
            os.fsync(handle.fileno())
            self._cache_published_header(mapping, generation, row_count)
        except (OSError, ValueError) as exc:
            raise StorageError(f"cannot publish fixed table header {self.path}: {exc}") from exc

    def _apply_entries_locked(self, entries: Sequence[tuple[int, bytes]]) -> None:
        handle, mapping = self._ensure_mapping()
        for slot, record in entries:
            offset = self._slot_offset(slot)
            mapping[offset : offset + self.record_size] = record
        _flush_mapped_ranges(
            mapping,
            [(self._slot_offset(slot), self.record_size) for slot, _record in entries],
        )
        os.fsync(handle.fileno())

    def _apply_entries_and_header_locked(
        self,
        entries: Sequence[tuple[int, bytes]],
        generation: int,
        row_count: int,
        *,
        sync: bool = True,
    ) -> None:
        """Publish records and their generation header.

        The WAL has already been fsynced by the caller.  ``sync=False`` keeps
        the mmap hot path free of table flush syscalls; the durable WAL remains
        the recovery source until a checkpoint calls this method with
        ``sync=True``.
        """

        slot_data = self._header_slot(self._header_body(generation, row_count))
        header_offset = (generation & 1) * _FIXED_HEADER_SLOT
        with self._mapping_guard:
            handle, mapping = self._ensure_mapping()
            for slot, record in entries:
                offset = self._slot_offset(slot)
                mapping[offset : offset + self.record_size] = record
            # Publish the generation header through the same mapping as the
            # slot writes.  One flush + fsync keeps the original WAL -> data
            # -> header durability order while avoiding a second file open and
            # buffered I/O.
            mapping[header_offset : header_offset + len(slot_data)] = slot_data
            if sync:
                _flush_mapped_ranges(
                    mapping,
                    [
                        *[(self._slot_offset(slot), len(record)) for slot, record in entries],
                        (header_offset, len(slot_data)),
                    ],
                )
                os.fsync(handle.fileno())
            self._cache_published_header(mapping, generation, row_count)

    def _recover_locked(self) -> None:
        if not self.active:
            return
        wal_size = self._wal_size()
        if wal_size == 0:
            header = self._read_header()
            self._wal_observed_size = 0
            self._wal_observed_tail_generation = None
            self._wal_observed_header_generation = header["generation"]
            return
        # The only way a writer changes a non-empty append-only WAL is by
        # appending bytes first.  When the size is unchanged, the last tail
        # probe already proved that the stream is covered; avoid a duplicate
        # header decode on every short write.
        if self._wal_observed_size == wal_size and self._wal_observed_tail_generation is not None:
            return
        header = self._read_header()
        if (
            self._wal_observed_size == wal_size
            and self._wal_observed_header_generation == header["generation"]
            and self._wal_observed_tail_generation is not None
            and self._wal_observed_tail_generation <= header["generation"]
        ):
            return
        tail_generation, tail_incomplete = self._read_wal_tail(wal_size)
        if not tail_incomplete and tail_generation is not None and tail_generation < header["generation"]:
            self._wal_observed_size = wal_size
            self._wal_observed_tail_generation = tail_generation
            self._wal_observed_header_generation = header["generation"]
            return
        records, valid_end, incomplete = self._read_wal()
        if not records and not incomplete:
            self._wal_observed_size = wal_size
            return
        changed = False
        recovered_entries: list[tuple[int, bytes]] = []
        recovered_generation = header["generation"]
        recovered_row_count = header["row_count"]
        for generation, row_count, entries in records:
            if generation < header["generation"]:
                continue
            if generation == header["generation"]:
                # A deferred table fsync permits the generation header to
                # reach stable storage before one of its record pages.  Do
                # not trust that header alone: compare the current-generation
                # WAL bytes with the mapped slots and repair any mismatch.
                matches = int(row_count) == int(header["row_count"])
                if matches and entries:
                    with self._mapped() as (_handle, mapping):
                        for slot, record in entries:
                            raw = memoryview(mapping)[
                                self._slot_offset(slot) : self._slot_offset(slot) + self.record_size
                            ]
                            try:
                                if bytes(raw) != record:
                                    matches = False
                                    break
                            finally:
                                raw.release()
                if matches:
                    continue
                recovered_entries.extend(entries)
                recovered_generation = generation
                recovered_row_count = row_count
                changed = True
                continue
            if generation != recovered_generation + 1:
                raise StorageError(f"fixed WAL sequence gap: {self.wal_path}")
            recovered_entries.extend(entries)
            recovered_generation = generation
            recovered_row_count = row_count
            changed = True
        if changed:
            self._apply_entries_and_header_locked(
                recovered_entries,
                recovered_generation,
                recovered_row_count,
            )
        if changed or incomplete:
            self._truncate_wal_locked(valid_end if incomplete else 0, known_nonempty=True)
        self._wal_observed_size = self._wal_size()
        self._wal_observed_tail_generation = recovered_generation if changed else (
            records[-1][0] if records else None
        )
        self._wal_observed_header_generation = recovered_generation if changed else header["generation"]
        if changed:
            with self._cache_guard:
                self._snapshot_cache = None
                self._sidecar_active_columns.clear()
            with self._key_cache_guard:
                self._key_cache.clear()
            with self._primary_slot_cache_guard:
                self._primary_slot_cache.clear()
            with self._row_cache_guard:
                self._row_cache.clear()
            with self._projection_cache_guard:
                self._projection_cache.clear()

    def _checkpoint_locked(self) -> None:
        """Flush the dirty fixed mapping and retire the durable WAL stream."""

        if self._wal_size() == 0:
            return
        try:
            with self._mapping_guard:
                handle, mapping = self._ensure_mapping()
                mapping.flush()
                os.fsync(handle.fileno())
            self._truncate_wal_locked(0, known_nonempty=True)
        except (OSError, ValueError) as exc:
            raise StorageError(f"cannot checkpoint fixed table {self.path}: {exc}") from exc

    def _recover_if_needed(self) -> None:
        if self._wal_write_active:
            return
        size = self._wal_size()
        if size <= 0:
            self._wal_observed_size = 0
            self._wal_observed_tail_generation = None
            return
        if self._wal_observed_size == size and self._wal_observed_tail_generation is not None:
            return
        header = self._read_header()
        if (
            self._wal_observed_size == size
            and self._wal_observed_header_generation == header["generation"]
            and self._wal_observed_tail_generation is not None
            and self._wal_observed_tail_generation <= header["generation"]
        ):
            return
        tail_generation, incomplete = self._read_wal_tail(size)
        if not incomplete and tail_generation is not None and tail_generation < header["generation"]:
            self._wal_observed_size = size
            self._wal_observed_tail_generation = tail_generation
            self._wal_observed_header_generation = header["generation"]
            return
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()

    def _build_key_index_from_map(self, mapping: mmap.mmap, key_columns: Sequence[str]) -> dict[tuple[Any, ...], tuple[int, ...]]:
        fields_by_name = self._fields_by_name
        try:
            # Preserve the caller's key-column order for composite indexes.
            fields = tuple(fields_by_name[column] for column in key_columns)
        except KeyError as exc:
            raise KeyError(exc.args[0]) from exc
        index: dict[tuple[Any, ...], list[tuple[int, int]]] = {}
        base_offset = _FIXED_DATA_OFFSET
        stride = self.record_size
        for slot in range(self.max_records):
            offset = base_offset + slot * stride
            # The state byte is the first byte of every fixed slot.  Skip
            # empty slots before allocating a memoryview and unpacking the
            # record header; large tables commonly have a substantial empty
            # tail reserved for future appends.
            if mapping[offset] == 0:
                continue
            order, values = self._read_index_entry(mapping, slot, fields, offset=offset)
            if values is None:
                continue
            key = values
            try:
                hash(key)
            except TypeError as exc:
                raise StorageError(f"unhashable fixed key in {self.path}") from exc
            index.setdefault(key, []).append((order, slot))
        # Physical slot order differs from insertion order after a delete and
        # slot reuse.  Store each bucket in persisted order once at index-build
        # time so equality writers can take the first ``LIMIT`` slots without
        # decoding every slot header and sorting on every call.
        return {
            key: tuple(slot for _order, slot in sorted(entries, key=lambda item: item[0]))
            for key, entries in index.items()
        }

    def _key_index(self, key_columns: Sequence[str], *, header: Optional[dict[str, int]] = None) -> dict[tuple[Any, ...], tuple[int, ...]]:
        self._recover_if_needed()
        key = tuple(key_columns)
        self._mark_sidecar_active(key)
        if header is None:
            header = self._read_header()
        with self._key_cache_guard.read():
            cached = self._key_cache.get(key)
            if cached is not None and cached[0] == header["generation"]:
                self._stats["index_hits"] += 1
                return cached[1]
        with self._index_build_guard_for(key):
            # Recheck after waiting for a concurrent builder.
            with self._key_cache_guard.read():
                cached = self._key_cache.get(key)
                if cached is not None and cached[0] == header["generation"]:
                    self._stats["index_hits"] += 1
                    return cached[1]
            persisted = self._load_persisted_key_index(key, header["generation"])
            if persisted is not None:
                with self._key_cache_guard:
                    self._key_cache[key] = (header["generation"], persisted)
                    self._stats["index_persisted_hits"] += 1
                return persisted
            with self._mapped() as (_handle, mapping):
                built = self._build_key_index_from_map(mapping, key)
            with self._key_cache_guard:
                self._key_cache[key] = (header["generation"], built)
                self._stats["index_rebuilds"] += 1
            self._persist_key_index(key, header["generation"], built)
            return built

    def _key_index_locked(
        self,
        mapping: mmap.mmap,
        generation: int,
        key_columns: Sequence[str],
        *,
        mark_active: bool = True,
    ) -> dict[tuple[Any, ...], tuple[int, ...]]:
        key = tuple(key_columns)
        if mark_active:
            self._mark_sidecar_active(key)
        with self._key_cache_guard.read():
            cached = self._key_cache.get(key)
            if cached is not None and cached[0] == generation:
                self._stats["index_hits"] += 1
                return cached[1]
        with self._index_build_guard_for(key):
            with self._key_cache_guard.read():
                cached = self._key_cache.get(key)
                if cached is not None and cached[0] == generation:
                    self._stats["index_hits"] += 1
                    return cached[1]
            persisted = self._load_persisted_key_index(key, generation)
            if persisted is not None:
                with self._key_cache_guard:
                    self._key_cache[key] = (generation, persisted)
                    self._stats["index_persisted_hits"] += 1
                return persisted
            built = self._build_key_index_from_map(mapping, key)
            with self._key_cache_guard:
                self._key_cache[key] = (generation, built)
                self._stats["index_rebuilds"] += 1
            self._persist_key_index(key, generation, built)
            return built

    def _find_slot(self, key_columns: Sequence[str], key: tuple[Any, ...], index: Optional[dict[tuple[Any, ...], tuple[int, ...]]] = None) -> int:
        positions = (self._key_index(key_columns) if index is None else index).get(key, ())
        if not positions:
            raise KeyError(key[0] if len(key) == 1 else key)
        if len(positions) != 1:
            raise ValueError(f"key {key[0] if len(key) == 1 else key!r} is not unique in fixed table {self.path}")
        return positions[0]

    def _is_primary_key(self, key_columns: Sequence[str]) -> bool:
        """Return whether a key shape is the declared unique primary key."""

        return bool(self.definition.primary_keys) and tuple(key_columns) == tuple(self.definition.primary_keys)

    def _cache_primary_slot(self, key: tuple[Any, ...], generation: int, slot: int) -> None:
        """Publish one direct primary-key slot in the bounded read cache."""

        with self._primary_slot_cache_guard:
            self._primary_slot_cache[key] = (int(generation), int(slot))
            self._primary_slot_cache.move_to_end(key)
            while len(self._primary_slot_cache) > _FIXED_PRIMARY_SLOT_CACHE_LIMIT:
                self._primary_slot_cache.popitem(last=False)
                self._stats["primary_slot_evictions"] += 1

    def _primary_slot(
        self,
        key: tuple[Any, ...],
        generation: int,
        *,
        index: Optional[dict[tuple[Any, ...], tuple[int, ...]]] = None,
        mapping: Optional[mmap.mmap] = None,
        header: Optional[Mapping[str, int]] = None,
    ) -> int:
        """Resolve a primary key directly to one slot when the cache is hot.

        The fallback still uses the authoritative equality index.  Caching one
        requested key at a time keeps the cold path cheap and avoids copying a
        potentially large index into a second map; repeated point reads then
        bypass ``dict[key] -> tuple(slot) -> uniqueness`` entirely.
        """

        self._mark_sidecar_active(self.definition.primary_keys)
        with self._primary_slot_cache_guard.read():
            cached = self._primary_slot_cache.get(key)
            if cached is not None and cached[0] == int(generation):
                self._stats["primary_slot_hits"] += 1
                return cached[1]
        self._stats["primary_slot_misses"] += 1
        primary_columns = tuple(self.definition.primary_keys)
        direct_valid = False
        direct_slot: Optional[int] = None
        # A full equality index may already be warm (for example after a
        # secondary-key operation or a concurrent first lookup).  Reuse it
        # before probing the side file; this keeps the probe a genuinely cold
        # path instead of adding one file check to every new primary key.
        if index is None:
            with self._key_cache_guard.read():
                cached_index = self._key_cache.get(primary_columns)
            if cached_index is not None and cached_index[0] == int(generation):
                index = cached_index[1]
        if index is None:
            # Serialise cold probes per primary index.  Several reader threads
            # arriving at an empty cache should share one payload parse rather
            # than each walking a 5-million-byte side file.  A malformed side
            # file is remembered for this generation so waiters go straight
            # to the single-flight full-index loader.
            probe_guard = self._index_build_guard_for(primary_columns)
            with probe_guard:
                with self._key_cache_guard.read():
                    cached_index = self._key_cache.get(primary_columns)
                if cached_index is not None and cached_index[0] == int(generation):
                    index = cached_index[1]
                else:
                    with self._cache_guard:
                        probe_invalid = self._primary_probe_invalid_generation == int(generation)
                    if not probe_invalid:
                        # A fresh single-threaded process usually needs one
                        # primary-key lookup before its direct slot cache is
                        # warm.  Under a burst of reader threads, however,
                        # decoding the complete index once is cheaper than
                        # making every worker wait behind a serialized probe
                        # parse.  The normal index-build guard still makes
                        # that fallback single-flight.
                        if threading.active_count() <= 2:
                            # Read just that key from the persisted immutable
                            # index instead of decoding every row into a
                            # Python dict.  The mapped record is checked when
                            # available; readers perform the same key check
                            # after decoding.
                            direct_valid, direct_slot = self._lookup_persisted_primary_slot(
                                key, int(generation)
                            )
                        else:
                            direct_valid = False
                        if not direct_valid:
                            with self._cache_guard:
                                self._primary_probe_invalid_generation = int(generation)
        if direct_valid:
            if direct_slot is None:
                raise KeyError(key[0] if len(key) == 1 else key)
            if mapping is not None:
                key_fields = tuple(self._fields_by_name[name] for name in primary_columns)
                _order, direct_row = self._read_record(mapping, direct_slot, fields=key_fields)
                if direct_row is not None and tuple(
                    direct_row.get(column) for column in primary_columns
                ) == key:
                    self._cache_primary_slot(key, int(generation), int(direct_slot))
                    return int(direct_slot)
                # The side index is an acceleration cache.  If a concurrent
                # slot reuse or an on-disk inconsistency disagrees with it,
                # use the full authoritative index path below.
            else:
                self._cache_primary_slot(key, int(generation), int(direct_slot))
                return int(direct_slot)
        if index is None:
            if mapping is not None:
                index = self._key_index_locked(mapping, int(generation), primary_columns)
            else:
                index = self._key_index(
                    primary_columns,
                    header=header if header is not None else {"generation": int(generation)},
                )
        positions = index.get(key, ())
        if not positions:
            raise KeyError(key[0] if len(key) == 1 else key)
        if len(positions) != 1:
            raise ValueError(f"key {key[0] if len(key) == 1 else key!r} is not unique in fixed table {self.path}")
        slot = int(positions[0])
        self._cache_primary_slot(key, int(generation), slot)
        return slot

    def _equal_slots_locked(
        self,
        mapping: mmap.mmap,
        generation: int,
        key_columns: Sequence[str],
        key: tuple[Any, ...],
        *,
        verify_records: bool = True,
        max_matches: Optional[int] = None,
    ) -> Optional[list[tuple[int, int]]]:
        """Return matching slots in insertion order for an equality map.

        ``None`` means the fixed backend cannot index the requested fields
        (for example an unknown or unhashable condition), allowing the
        relation layer to retain its general snapshot implementation.
        """

        try:
            hash(key)
        except TypeError:
            return None
        try:
            index = self._key_index_locked(mapping, generation, key_columns)
        except KeyError:
            return None
        except StorageError as exc:
            # Object/list columns can be valid fixed fields but cannot serve
            # as hash indexes.  Preserve the general snapshot fallback for
            # that case while still surfacing CRC/layout corruption.
            if "unhashable fixed key" in str(exc):
                return None
            raise
        positions = index.get(key, ())
        matches: list[tuple[int, int]] = []
        for slot in positions:
            if verify_records:
                # Readers may race a writer from another process after the
                # generation/index check, so retain the CRC-checked record
                # validation on the lock-free path.
                order, row = self._read_record(mapping, slot, fields=())
                if row is not None:
                    matches.append((order, slot))
            else:
                # Writers already hold the inter-process table lock.  The
                # index was built from this unchanged generation and stores
                # positions in insertion order, so defer all CRC/header work
                # to the full record read below.  The order value is ignored
                # by update/delete callers.
                matches.append((0, slot))
            if max_matches is not None and len(matches) >= max_matches:
                break
        # ``_build_key_index_from_map`` and its incremental cache updates keep
        # every bucket in persisted insertion order.  Avoid sorting the whole
        # secondary-key bucket again; this matters for LIMIT 1 updates where
        # a non-primary value may match many rows.
        return matches

    def select_equal(
        self,
        key_columns: Sequence[str],
        key: tuple[Any, ...],
        *,
        columns: Optional[Sequence[str]] = None,
        order_by: Optional[Union[str, Sequence[Any]]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        _retry: int = 0,
    ) -> Optional[list[dict[str, Any]]]:
        """Select equality matches without materialising a table snapshot."""

        if limit == 0:
            return []
        self._recover_if_needed()
        header = self._read_header()
        normalised_key_columns = tuple(_unquote_identifier(str(column)) for column in key_columns)
        normalised_key = tuple(key)
        selected = None if columns is None else tuple(_unquote_identifier(str(column)) for column in columns)
        requested_limit = limit
        requested_offset = offset
        order_items = _parse_order_by(order_by)
        field_map = {field.name: field for field in self.fields}
        requested = tuple(self.definition.column_names) if selected is None else selected
        needed_names = tuple(dict.fromkeys(normalised_key_columns + requested + tuple(item[0] for item in order_items)))
        if any(name not in field_map for name in needed_names):
            return None
        decode_fields = tuple(field_map[name] for name in needed_names)
        # Equality indexes are persisted in insertion order.  For an
        # unordered window, no candidate beyond the end of the requested
        # window can affect the result.  The generation check after the read
        # closes the race where another process changes an earlier slot while
        # this lock-free reader is traversing the bucket.
        candidate_limit = None if order_items or limit is None else offset + limit
        with self._mapped() as (_handle, mapping):
            matches = self._equal_slots_locked(
                mapping,
                header["generation"],
                normalised_key_columns,
                normalised_key,
                # The decode loop below reads each candidate record anyway.
                # Let that single read perform the CRC/state validation and
                # predicate re-check; validating the whole bucket here would
                # read every matching slot twice on projected equality
                # queries (the dominant cost for non-primary lookups).
                verify_records=False,
                max_matches=candidate_limit,
            )
            if matches is None:
                return None
            decode_matches = matches
            if not order_items:
                # Equality indexes already return insertion order.  Apply the
                # window before decoding records so ``LIMIT 1`` does not
                # deserialize every row sharing a secondary key.
                stop = None if limit is None else offset + limit
                decode_matches = matches[offset:stop]
                offset = 0
                limit = None
            rows: list[dict[str, Any]] = []
            for order, slot in decode_matches:
                _current_order, row = self._read_record(mapping, slot, fields=decode_fields)
                if row is not None and tuple(row.get(column) for column in normalised_key_columns) == normalised_key:
                    rows.append(row)
        if self._read_header()["generation"] != header["generation"] and _retry < 2:
            # A writer published while the lock-free read was in progress.
            # Re-evaluate the same query against the new generation rather
            # than returning a window based on a stale index prefix.
            return self.select_equal(
                normalised_key_columns,
                normalised_key,
                columns=selected,
                order_by=order_by,
                limit=requested_limit,
                offset=requested_offset,
                _retry=_retry + 1,
            )
        # ``matches`` is already in insertion order.  Sorting by ORDER BY is
        # stable, so ties retain the same persisted order as snapshot reads.
        _sort_rows(rows, order_by)
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        if selected is not None:
            rows = [{column: row.get(column) for column in selected} for row in rows]
        else:
            # The decode set starts with predicate and ORDER BY fields to
            # support projected reads.  Rebuild the default result in schema
            # order so the fixed path has the same observable column order as
            # the snapshot path and the web API.
            rows = [
                {column: row.get(column) for column in self.definition.column_names}
                for row in rows
            ]
        return [self._detach_row(row) for row in rows]

    def update_equal(
        self,
        key_columns: Sequence[str],
        key: tuple[Any, ...],
        patch: Mapping[str, Any],
        *,
        limit: Optional[int] = None,
    ) -> Optional[int]:
        """Patch equality matches through one fixed-table WAL transaction."""

        if limit == 0:
            return 0
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            with self._mapped() as (_handle, mapping):
                matches = self._equal_slots_locked(
                    mapping,
                    header["generation"],
                    key_columns,
                    key,
                    verify_records=False,
                    max_matches=limit,
                )
                if matches is None:
                    return None
                selected = matches if limit is None else matches[:limit]
                entries: list[tuple[int, bytes]] = []
                primary_keys = self.definition.primary_keys
                patch_values = dict(patch)
                primary_changed = bool(set(patch_values).intersection(primary_keys))
                primary_index: Optional[dict[tuple[Any, ...], tuple[int, ...]]] = None
                used_primary: set[tuple[Any, ...]] = set()
                if primary_changed:
                    primary_index = self._key_index_locked(mapping, header["generation"], primary_keys)
                    used_primary = set(primary_index)
                if not primary_changed:
                    self._validate_patch(patch_values)
                    for _order, slot in selected:
                        _current_order, record = self._patch_mapped_record(mapping, slot, patch_values)
                        entries.append((slot, record))
                else:
                    for _order, slot in selected:
                        current_order, current = self._read_record(mapping, slot)
                        if current is None:
                            continue
                        updated = dict(current)
                        updated.update(patch_values)
                        self._validate_row(updated)
                        assert primary_index is not None
                        updated_primary = tuple(updated.get(column) for column in primary_keys)
                        current_primary = tuple(current.get(column) for column in primary_keys)
                        if updated_primary != current_primary:
                            conflicting = primary_index.get(updated_primary, ())
                            if any(candidate != slot for candidate in conflicting) or (updated_primary in used_primary and updated_primary != current_primary):
                                raise SchemaError(f"duplicate primary key {updated_primary!r}")
                            used_primary.discard(current_primary)
                            used_primary.add(updated_primary)
                        entries.append((slot, self._encode_record(updated, current_order)))
            if not entries:
                return 0
            # An update that touches no column used by any cached equality
            # index leaves every cached slot map valid.  Advancing their
            # generation here avoids rebuilding a full max-elements scan for
            # each repeated non-primary condition update.  If a key column is
            # patched, invalidate conservatively so the next operation sees
            # the new value.
            changed_columns = {str(column) for column in patch}
            with self._key_cache_guard.read():
                preserve_key_cache = all(
                    not changed_columns.intersection(cache_key)
                    for cache_key in self._key_cache
                )
            self._commit_locked(
                entries,
                header["row_count"],
                header=header,
                preserve_key_cache=preserve_key_cache,
                persisted_index_changed_columns=changed_columns,
            )
            return len(entries)

    def update_equal_many(
        self,
        operations: Sequence[tuple[Sequence[str], tuple[Any, ...], Mapping[str, Any]]],
    ) -> Optional[int]:
        """Apply several equality updates through one durable WAL frame.

        The caller supplies already-normalised equality keys and patches.  All
        matching slots are resolved while one table lock is held; the complete
        set is then published atomically with the same WAL and CRC protocol as
        a single update.  ``None`` requests the relation-layer fallback when a
        condition cannot be represented by a fixed hash index.
        """

        if not operations:
            return 0
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            entries_by_slot: dict[int, tuple[int, bytes]] = {}
            rows_by_slot: dict[int, tuple[int, dict[str, Any]]] = {}
            changed_columns: set[str] = set()
            primary_keys = self.definition.primary_keys
            primary_changed = any(
                set(str(column) for column in patch).intersection(primary_keys)
                for _columns, _key, patch in operations
            )
            primary_operations = any(
                tuple(columns) == tuple(primary_keys)
                for columns, _key, _patch in operations
            )
            primary_index: Optional[dict[tuple[Any, ...], tuple[int, ...]]] = None
            used_primary: set[tuple[Any, ...]] = set()
            with self._mapped() as (_handle, mapping):
                if primary_changed or primary_operations:
                    primary_index = self._key_index_locked(mapping, header["generation"], primary_keys)
                    used_primary = set(primary_index)
                for key_columns, key, patch in operations:
                    # Primary-key batch operations are already unique and can
                    # address the cached slot tuple directly.  This avoids
                    # rebuilding the same one-element equality result for
                    # every item while retaining the generic path for
                    # secondary and composite equality conditions.
                    if tuple(key_columns) == tuple(primary_keys):
                        assert primary_index is not None
                        try:
                            positions = primary_index.get(key, ())
                        except TypeError:
                            # Keep the relation-layer fallback for a JSON
                            # value that cannot be used as a hash key.
                            return None
                        matches = [(0, slot) for slot in positions]
                    else:
                        matches = self._equal_slots_locked(
                            mapping,
                            header["generation"],
                            key_columns,
                            key,
                            verify_records=False,
                        )
                    if matches is None:
                        return None
                    # The public Table/KeyValue layers already detach and
                    # validate patches before reaching the fixed store. A
                    # second recursive copy only adds latency to the hot
                    # single-row update path.
                    patch_values = dict(patch)
                    changed_columns.update(str(column) for column in patch_values)
                    patch_changes_primary = bool(set(patch_values).intersection(primary_keys))
                    if not patch_changes_primary:
                        self._validate_patch(patch_values)
                    for _order, slot in matches:
                        if slot in rows_by_slot or slot in entries_by_slot:
                            raise ValueError(f"batch equality updates target the same slot more than once: {slot}")
                        if not patch_changes_primary:
                            current_order, record = self._patch_mapped_record(mapping, slot, patch_values)
                            entries_by_slot[slot] = (slot, record)
                            continue
                        current_order, current = self._read_record(mapping, slot)
                        if current is None:
                            continue
                        updated = dict(current)
                        updated.update(patch_values)
                        self._validate_row(updated)
                        if primary_changed:
                            assert primary_index is not None
                            updated_primary = tuple(updated.get(column) for column in primary_keys)
                            current_primary = tuple(current.get(column) for column in primary_keys)
                            if updated_primary != current_primary:
                                conflicting = primary_index.get(updated_primary, ())
                                if any(candidate != slot for candidate in conflicting) or (
                                    updated_primary in used_primary and updated_primary != current_primary
                                ):
                                    raise SchemaError(f"duplicate primary key {updated_primary!r}")
                                used_primary.discard(current_primary)
                                used_primary.add(updated_primary)
                        rows_by_slot[slot] = (current_order, updated)
                for slot, (order, row) in rows_by_slot.items():
                    entries_by_slot[slot] = (slot, self._encode_record(row, order))
            if not entries_by_slot:
                return 0
            with self._key_cache_guard.read():
                preserve_key_cache = all(
                    not changed_columns.intersection(cache_key)
                    for cache_key in self._key_cache
                )
            self._commit_locked(
                list(entries_by_slot.values()),
                header["row_count"],
                header=header,
                preserve_key_cache=preserve_key_cache,
                persisted_index_changed_columns=changed_columns,
            )
            return len(entries_by_slot)

    def delete_equal(
        self,
        key_columns: Sequence[str],
        key: tuple[Any, ...],
        *,
        limit: Optional[int] = None,
    ) -> Optional[int]:
        """Delete equality matches through one fixed-table WAL transaction."""

        if limit == 0:
            return 0
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            with self._mapped() as (_handle, mapping):
                matches = self._equal_slots_locked(
                    mapping,
                    header["generation"],
                    key_columns,
                    key,
                    verify_records=False,
                    max_matches=limit,
                )
                if matches is None:
                    return None
                selected = matches if limit is None else matches[:limit]
                slots = [slot for _order, slot in selected]
            if not slots:
                return 0
            self._commit_locked(
                [(slot, bytes(self.record_size)) for slot in slots],
                header["row_count"] - len(slots),
                header=header,
                preserve_key_cache=True,
                key_cache_removals=slots,
                persisted_index_operation="remove",
            )
            return len(slots)

    def delete_equal_many(
        self,
        operations: Sequence[tuple[Sequence[str], tuple[Any, ...]]],
    ) -> Optional[int]:
        """Delete several equality matches through one durable WAL frame."""

        if not operations:
            return 0
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            slots: list[int] = []
            seen: set[int] = set()
            with self._mapped() as (_handle, mapping):
                primary_keys = self.definition.primary_keys
                primary_operations = any(
                    tuple(columns) == tuple(primary_keys)
                    for columns, _key in operations
                )
                primary_index = (
                    self._key_index_locked(mapping, header["generation"], primary_keys)
                    if primary_operations
                    else None
                )
                for key_columns, key in operations:
                    if tuple(key_columns) == tuple(primary_keys):
                        assert primary_index is not None
                        try:
                            positions = primary_index.get(key, ())
                        except TypeError:
                            return None
                        matches = [(0, slot) for slot in positions]
                    else:
                        matches = self._equal_slots_locked(
                            mapping,
                            header["generation"],
                            key_columns,
                            key,
                            verify_records=False,
                        )
                    if matches is None:
                        return None
                    for _order, slot in matches:
                        if slot in seen:
                            raise ValueError(f"batch equality deletes target the same slot more than once: {slot}")
                        seen.add(slot)
                        slots.append(slot)
            if not slots:
                return 0
            self._commit_locked(
                [(slot, bytes(self.record_size)) for slot in slots],
                header["row_count"] - len(slots),
                header=header,
                preserve_key_cache=True,
                key_cache_removals=slots,
                persisted_index_operation="remove",
            )
            return len(slots)

    def _free_slot(self, mapping: mmap.mmap, preferred: int = 0) -> int:
        """Find a free slot, starting at the expected append position."""

        preferred = max(0, min(int(preferred), self.max_records - 1)) if self.max_records else 0
        for slot in range(preferred, self.max_records):
            if mapping[self._slot_offset(slot)] == 0:
                return slot
        for slot in range(0, preferred):
            if mapping[self._slot_offset(slot)] == 0:
                return slot
        raise SchemaError(f"table {self.definition.eng_name!r} reached fixed storage capacity {self.max_records}")

    def _commit_locked(
        self,
        entries: Sequence[tuple[int, bytes]],
        row_count: int,
        generation: Optional[int] = None,
        *,
        header: Optional[Mapping[str, int]] = None,
        preserve_key_cache: bool = False,
        key_cache_additions: Optional[Any] = None,
        key_cache_removals: Optional[Sequence[int]] = None,
        persisted_index_changed_columns: Optional[set[str]] = None,
        persisted_index_operation: Optional[str] = None,
    ) -> None:
        # Most callers have already recovered and read the header while they
        # held the table lock.  Reusing that immutable header avoids a second
        # WAL probe and header read on every short transaction.  The default
        # path remains for snapshot replacement and recovery callers.
        if header is None:
            self._recover_locked()
            header = self._read_header()
        previous_generation = int(header["generation"])
        next_generation = previous_generation + 1 if generation is None else generation
        # Deletions used to rebuild every cached equality bucket by scanning
        # the complete index.  A primary-key cache with thousands of entries
        # therefore made each single-row delete O(n), even though the slot to
        # remove is already known.  Capture the affected key(s) while the old
        # record is still present so cache maintenance can remove only those
        # positions after publication.  Caches from an older generation are
        # deliberately not preserved: another process may have changed their
        # key values while this process was idle.
        removal_keys_by_cache: dict[
            tuple[str, ...], dict[tuple[Any, ...], set[int]]
        ] = {}
        removals = tuple(dict.fromkeys(key_cache_removals or ()))
        if preserve_key_cache and removals:
            with self._key_cache_guard.read():
                cached_items = tuple(self._key_cache.items())
            current_generation = int(header["generation"])
            current_items = tuple(
                (cache_key, index)
                for cache_key, (cache_generation, index) in cached_items
                if cache_generation == current_generation
            )
            if current_items:
                with self._mapped() as (_handle, mapping):
                    for cache_key, _index in current_items:
                        fields = tuple(self._fields_by_name[name] for name in cache_key)
                        by_key: dict[tuple[Any, ...], set[int]] = {}
                        valid = True
                        for slot in removals:
                            _order, row = self._read_record(mapping, slot, fields=fields)
                            if row is None:
                                continue
                            removed_key = tuple(row[name] for name in cache_key)
                            try:
                                hash(removed_key)
                            except TypeError:
                                valid = False
                                break
                            by_key.setdefault(removed_key, set()).add(slot)
                        if valid:
                            removal_keys_by_cache[cache_key] = by_key
            if persisted_index_operation == "remove":
                # Include side indexes created by other processes.  Their
                # metadata is cheap to inspect, and reading the old key fields
                # before zeroing the slots lets us append remove frames even
                # when this writer has no in-process key-cache entry.
                for cache_key, _path, _descriptor in self._persisted_index_descriptors(current_generation):
                    if cache_key in removal_keys_by_cache:
                        continue
                    try:
                        fields = tuple(self._fields_by_name[name] for name in cache_key)
                        by_key: dict[tuple[Any, ...], set[int]] = {}
                        with self._mapped() as (_handle, mapping):
                            for slot in removals:
                                _order, row = self._read_record(mapping, slot, fields=fields)
                                if row is None:
                                    continue
                                removed_key = tuple(row[name] for name in cache_key)
                                hash(removed_key)
                                by_key.setdefault(removed_key, set()).add(slot)
                        removal_keys_by_cache[cache_key] = by_key
                    except (KeyError, StorageError, TypeError, ValueError):
                        continue
        self._wal_write_active = True
        try:
            self._append_wal_locked(next_generation, row_count, entries)
            # The WAL frame is already durable.  Publish the mapped record and
            # generation immediately for hot readers, but defer the expensive
            # table flush until the bounded Redis/AOF-style checkpoint below.
            self._apply_entries_and_header_locked(
                entries,
                next_generation,
                row_count,
                sync=False,
            )
            self._wal_observed_header_generation = int(next_generation)
            wal_position = self._wal_position
            if wal_position is not None and wal_position >= _FIXED_WAL_CHECKPOINT_BYTES:
                self._checkpoint_locked()
        finally:
            self._wal_write_active = False
        # The side index is an optional cache.  Advance only indexes whose key
        # columns were not modified; inserts, deletes and full replacements
        # leave every side file at the previous generation so a future reader
        # will rebuild it from the authoritative table.
        self._advance_persisted_indexes(
            previous_generation,
            int(next_generation),
            persisted_index_changed_columns,
        )
        # A value-only local commit does not change the encoded primary-key
        # side-index payload.  Advance its already-validated probe map with
        # the table generation so a sequence of single-row updates avoids
        # reopening the index file after every fsync.  Structural operations
        # (insert/delete or a primary-key change) intentionally leave the
        # probe stale and let the next lookup validate/rebuild it.
        primary_columns = tuple(self.definition.primary_keys)
        primary_unchanged = (
            persisted_index_operation is None
            and persisted_index_changed_columns is not None
            and not set(primary_columns).intersection(persisted_index_changed_columns)
        )
        if primary_unchanged:
            with self._cache_guard:
                probe = self._primary_probe_cache
                if probe is not None and probe[0] == previous_generation:
                    self._primary_probe_cache = (
                        int(next_generation),
                        probe[1],
                        probe[2],
                        probe[3],
                    )
        if isinstance(key_cache_additions, Mapping):
            additions_by_columns = {
                tuple(columns): list(additions.items() if isinstance(additions, Mapping) else additions)
                for columns, additions in key_cache_additions.items()
            }
        elif key_cache_additions is not None:
            additions_by_columns = {
                tuple(key_cache_additions[0]): list(key_cache_additions[1].items()
                    if isinstance(key_cache_additions[1], Mapping) else key_cache_additions[1])
            }
        else:
            additions_by_columns = {}
        compact_index_keys = self._append_persisted_index_deltas(
            previous_generation,
            int(next_generation),
            additions_by_columns,
            removal_keys_by_cache,
            entries,
            persisted_index_operation,
        )
        # Decode each newly published slot at most once when a local
        # value-only commit can preserve hot rows.  This lets the LRU retain
        # unrelated Redis-style hot keys and refresh only entries that point
        # at an affected slot; a key change or an externally observed
        # generation still drops the cache conservatively.
        decoded_entries: dict[int, tuple[int, Optional[dict[str, Any]]]] = {}
        if preserve_key_cache:
            with self._row_cache_guard.read():
                cached_slots = {cached[1] for cached in self._row_cache.values()}
            for slot, record in entries:
                if int(slot) in cached_slots:
                    decoded_entries[int(slot)] = self._decode_record(record)
        compacted_cache_updates: list[
            tuple[tuple[str, ...], dict[tuple[Any, ...], tuple[int, ...]]]
        ] = []
        with self._row_cache_guard:
            previous_row_cache = self._row_cache
            self._row_cache = OrderedDict()
            if preserve_key_cache:
                for row_key, (old_generation, cached_slot, cached_row) in previous_row_cache.items():
                    # Do not carry a cache across an external commit that was
                    # not observed through this operation's header.
                    if old_generation != int(header["generation"]):
                        continue
                    replacement = decoded_entries.get(cached_slot)
                    if replacement is not None:
                        _order, replacement_row = replacement
                        if replacement_row is None:
                            continue
                        key_columns, key = row_key
                        if tuple(replacement_row.get(column) for column in key_columns) != key:
                            # The indexed key changed; the old hot entry must
                            # never be returned under its previous key.
                            continue
                        cached_row = replacement_row
                    self._row_cache[row_key] = (int(next_generation), cached_slot, cached_row)
                    self._row_cache.move_to_end(row_key)
                    if len(self._row_cache) > _FIXED_ROW_CACHE_LIMIT:
                        self._row_cache.popitem(last=False)
        # Preserve projected hot rows across a local commit whenever their
        # slot and requested fields are unaffected.  Refresh only projections
        # whose requested fields were touched; this keeps mixed read/write
        # workloads warm without trusting a cache across an external commit.
        projection_refresh_slots: set[int] = set()
        with self._projection_cache_guard.read():
            previous_projection_cache = tuple(self._projection_cache.items())
        changed_projection_columns = persisted_index_changed_columns
        if preserve_key_cache and previous_projection_cache:
            for _projection_key, (old_generation, cached_slot, _cached_row) in previous_projection_cache:
                if old_generation != int(header["generation"]):
                    continue
                requested_fields = _projection_key[2]
                if int(cached_slot) in removals or (
                    changed_projection_columns is not None
                    and set(requested_fields).intersection(changed_projection_columns)
                ):
                    projection_refresh_slots.add(int(cached_slot))
        decoded_projection_entries: dict[int, tuple[int, Optional[dict[str, Any]]]] = {}
        if projection_refresh_slots:
            for slot, record in entries:
                if int(slot) in projection_refresh_slots:
                    decoded_projection_entries[int(slot)] = self._decode_record(record)
        with self._projection_cache_guard:
            self._projection_cache = OrderedDict()
            if preserve_key_cache:
                for projection_key, (old_generation, cached_slot, cached_row) in previous_projection_cache:
                    if old_generation != int(header["generation"]):
                        continue
                    if int(cached_slot) in removals:
                        continue
                    replacement = decoded_projection_entries.get(int(cached_slot))
                    if replacement is not None:
                        _order, replacement_row = replacement
                        if replacement_row is None:
                            continue
                        key_columns, expected_key, requested_fields = projection_key
                        if tuple(replacement_row.get(column) for column in key_columns) != expected_key:
                            continue
                        cached_row = {
                            name: replacement_row.get(name)
                            for name in requested_fields
                        }
                    self._projection_cache[projection_key] = (
                        int(next_generation),
                        int(cached_slot),
                        cached_row,
                    )
                    self._projection_cache.move_to_end(projection_key)
                    if len(self._projection_cache) > _FIXED_PROJECTION_CACHE_LIMIT:
                        self._projection_cache.popitem(last=False)
                        self._stats["projection_cache_evictions"] += 1
        with self._cache_guard:
            self._snapshot_cache = None
        with self._key_cache_guard:
            if preserve_key_cache:
                updated_cache: dict[tuple[str, ...], tuple[int, dict[tuple[Any, ...], tuple[int, ...]]]] = {}
                removal_set = set(removals)
                for cache_key, (old_generation, index) in self._key_cache.items():
                    # Do not carry a cache across an external commit that was
                    # not observed through this particular index.  Rebuilding
                    # it lazily is cheaper than returning stale key results.
                    if old_generation != int(header["generation"]):
                        continue
                    updated_index = index
                    if removal_set:
                        targeted = removal_keys_by_cache.get(cache_key)
                        if targeted is not None:
                            for removed_key, removed_slots in targeted.items():
                                positions = index.get(removed_key, ())
                                if not positions:
                                    continue
                                remaining = tuple(slot for slot in positions if slot not in removed_slots)
                                if remaining:
                                    if remaining != positions:
                                        if updated_index is index:
                                            updated_index = dict(index)
                                        updated_index[removed_key] = remaining
                                elif updated_index is index:
                                    updated_index = dict(index)
                                if not remaining:
                                    updated_index.pop(removed_key, None)
                        else:
                            # This is only a defensive fallback for an
                            # unusual cache whose key could not be decoded.
                            updated_index = {
                                key: tuple(slot for slot in positions if slot not in removal_set)
                                for key, positions in index.items()
                                if any(slot not in removal_set for slot in positions)
                            }
                    additions = additions_by_columns.get(cache_key, ())
                    if additions:
                        if updated_index is index:
                            updated_index = dict(index)
                        for key, slot in additions:
                            updated_index[key] = tuple(updated_index.get(key, ())) + (slot,)
                    updated_cache[cache_key] = (next_generation, updated_index)
                self._key_cache = updated_cache
                compacted_cache_updates = [
                    (cache_key, cache_index)
                    for cache_key, (_cache_generation, cache_index) in updated_cache.items()
                    if cache_key in compact_index_keys
                ]
            else:
                self._key_cache.clear()
        # Keep the direct primary-key -> slot cache in lockstep with the
        # generation transition.  This cache is intentionally maintained
        # separately from the general index so a hot point update can advance
        # only the few keys requested by this process; external generations
        # are rejected by the generation check and are cleared lazily here.
        primary_columns = tuple(self.definition.primary_keys)
        with self._primary_slot_cache_guard:
            if preserve_key_cache:
                primary_removals: set[int] = set(removals)
                targeted_primary = removal_keys_by_cache.get(primary_columns)
                if targeted_primary:
                    primary_removals.update(
                        slot
                        for slots in targeted_primary.values()
                        for slot in slots
                    )
                self._primary_slot_cache = OrderedDict(
                    (key, (int(next_generation), slot))
                    for key, (old_generation, slot) in self._primary_slot_cache.items()
                    if old_generation == int(header["generation"]) and slot not in primary_removals
                )
                # Do not eagerly populate the direct map for every inserted
                # key.  The general index already carries the full batch;
                # adding thousands of entries here would turn the first
                # post-append read into an avoidable cache build.  A key is
                # inserted into this smaller cache only when a caller actually
                # requests it.
            else:
                self._primary_slot_cache.clear()
        # Compact only after an append-only delta stream crosses its bounded
        # threshold.  Ordinary inserts/deletes therefore update a handful of
        # bytes; the occasional full rewrite keeps reopen time and side-file
        # size bounded without entering the durable table commit path.
        compacted_keys = {cache_key for cache_key, _cache_index in compacted_cache_updates}
        for cache_key, cache_index in compacted_cache_updates:
            self._persist_key_index(cache_key, int(next_generation), cache_index)
        for cache_key in compact_index_keys - compacted_keys:
            rebuilt = self._load_persisted_key_index(cache_key, int(next_generation))
            if rebuilt is not None:
                self._persist_key_index(cache_key, int(next_generation), rebuilt)

    def _row_for_slot(self, mapping: mmap.mmap, slot: int) -> dict[str, Any]:
        _order, row = self._read_record(mapping, slot)
        if row is None:
            raise KeyError(slot)
        return row

    def _detach_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Detach a decoded row without deep-copying fixed scalar fields."""

        detached = dict(row)
        # Pickled columns may contain arbitrary mutable objects.  Primitive
        # fixed-width values are immutable and can be copied by reference,
        # which avoids a full recursive deepcopy on every indexed read.
        for field in self.fields:
            if field.kind not in {"int", "float", "bool", "str"} and field.name in detached:
                detached[field.name] = copy.deepcopy(detached[field.name])
        return detached

    def get(
        self,
        key_columns: Sequence[str],
        key: tuple[Any, ...],
        *,
        fields: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        self._stats["get_calls"] += 1
        requested: Optional[tuple[str, ...]] = None
        decode_fields: Optional[tuple[_FixedField, ...]] = None
        if fields is not None:
            requested = tuple(str(field) for field in fields)
            field_map = {field.name: field for field in self.fields}
            if all(name in field_map for name in requested):
                names = tuple(dict.fromkeys(tuple(str(column) for column in key_columns) + requested))
                decode_fields = tuple(field_map[name] for name in names)
        key_columns_tuple = tuple(key_columns)
        projection_cache_key = (
            (key_columns_tuple, key, requested)
            if requested is not None and decode_fields is not None
            else None
        )
        for _attempt in range(3):
            if _attempt:
                self._stats["generation_retries"] += 1
            self._recover_if_needed()
            header = self._read_header()
            cache_key = (key_columns_tuple, key)
            if projection_cache_key is not None:
                with self._projection_cache_guard.read():
                    cached_projection = self._projection_cache.get(projection_cache_key)
                if (
                    cached_projection is not None
                    and cached_projection[0] == header["generation"]
                ):
                    self._stats["projection_cache_hits"] += 1
                    return self._detach_row(cached_projection[2])
                self._stats["projection_cache_misses"] += 1
            with self._row_cache_guard.read():
                cached_row = self._row_cache.get(cache_key)
                if cached_row is not None and cached_row[0] == header["generation"]:
                    self._stats["row_cache_hits"] += 1
                else:
                    self._stats["row_cache_misses"] += 1
            if cached_row is not None and cached_row[0] == header["generation"]:
                if requested is None or decode_fields is None:
                    return self._detach_row(cached_row[2])
                return self._detach_row({name: cached_row[2].get(name) for name in requested})
            if self._is_primary_key(key_columns_tuple):
                # The primary key is unique by definition.  Once this key has
                # been observed, resolve it through the direct slot cache and
                # avoid the general ``key -> tuple(slots)`` adapter on every
                # point read.  The mmap record and generation checks below
                # remain authoritative for hot-sync safety.
                slot = self._primary_slot(key, header["generation"], header=header)
            else:
                index = self._key_index(key_columns, header=header)
                slot = self._find_slot(key_columns, key, index)
            try:
                with self._mapped() as (_handle, mapping):
                    row = self._read_record(mapping, slot, fields=decode_fields)[1]
                    if row is None:
                        raise KeyError(slot)
                if tuple(row.get(column) for column in key_columns) == key:
                    if requested is not None and decode_fields is not None:
                        projected = {name: row.get(name) for name in requested}
                        assert projection_cache_key is not None
                        self._projection_cache_put(
                            projection_cache_key,
                            header["generation"],
                            slot,
                            projected,
                        )
                        return self._detach_row(projected)
                    self._cache_put(cache_key, header["generation"], slot, row)
                    return self._detach_row(row)
                # A concurrent delete/reinsert reused the old slot. Rebuild
                # the index on the next attempt.
            except StorageError:
                if _attempt == 2:
                    raise
                time.sleep(0.001 * (_attempt + 1))
        raise StorageError(f"fixed table changed while reading: {self.path}")

    def get_many(
        self,
        key_columns: Sequence[str],
        keys: Sequence[tuple[Any, ...]],
        *,
        with_generation: bool = False,
    ) -> Any:
        self._stats["get_many_calls"] += 1
        cache_key_columns = tuple(key_columns)
        requested_keys = tuple(keys)
        if not requested_keys:
            return ({}, self._read_header()["generation"]) if with_generation else {}
        for _attempt in range(3):
            if _attempt:
                self._stats["generation_retries"] += 1
            self._recover_if_needed()
            header = self._read_header()
            index = self._key_index(key_columns, header=header)
            # Inspect the whole request under one cache lock.  Apart from
            # reducing lock traffic for MGET-sized reads, this lets us dedupe
            # duplicate keys before touching the mmap and then write all cold
            # rows back to the LRU in one critical section.
            result: dict[Any, dict[str, Any]] = {}
            cached_rows: dict[tuple[tuple[str, ...], tuple[Any, ...]], dict[str, Any]] = {}
            pending: dict[tuple[tuple[str, ...], tuple[Any, ...]], tuple[int, tuple[Any, ...]]] = {}
            key_public: dict[tuple[tuple[str, ...], tuple[Any, ...]], Any] = {}
            with self._row_cache_guard.read():
                for key in requested_keys:
                    positions = index.get(key, ())
                    if not positions:
                        continue
                    if len(positions) != 1:
                        raise ValueError(f"key {key!r} is not unique in fixed table {self.path}")
                    public_key = key[0] if len(key) == 1 else key
                    cache_key = (cache_key_columns, key)
                    key_public[cache_key] = public_key
                    cached = self._row_cache.get(cache_key)
                    if cached is not None and cached[0] == header["generation"] and cached[1] == positions[0]:
                        self._stats["row_cache_hits"] += 1
                        cached_rows[cache_key] = cached[2]
                    else:
                        self._stats["row_cache_misses"] += 1
                        pending.setdefault(cache_key, (positions[0], key))
            loaded_rows: dict[tuple[tuple[str, ...], tuple[Any, ...]], dict[str, Any]] = {}
            if pending:
                try:
                    with self._mapped() as (_handle, mapping):
                        for cache_key, (slot, key) in pending.items():
                            row = self._row_for_slot(mapping, slot)
                            if tuple(row.get(column) for column in key_columns) != key:
                                raise StorageError(f"fixed table changed while reading: {self.path}")
                            loaded_rows[cache_key] = row
                except KeyError as exc:
                    if _attempt == 2:
                        raise StorageError(f"fixed table changed while reading: {self.path}") from exc
                    continue
                except StorageError:
                    if _attempt == 2:
                        raise
                    continue
                with self._row_cache_guard:
                    for cache_key, row in loaded_rows.items():
                        slot, _key = pending[cache_key]
                        self._row_cache[cache_key] = (header["generation"], slot, row)
                        self._row_cache.move_to_end(cache_key)
                        while len(self._row_cache) > _FIXED_ROW_CACHE_LIMIT:
                            self._row_cache.popitem(last=False)
                            self._stats["row_cache_evictions"] += 1
            rows_by_cache = {**cached_rows, **loaded_rows}
            # Reassemble in caller order.  The cache dictionaries are grouped
            # by hit/miss for the batched critical sections above, so iterating
            # them directly would reorder an MGET request.
            for key in requested_keys:
                if not index.get(key, ()):
                    continue
                cache_key = (cache_key_columns, key)
                row = rows_by_cache.get(cache_key)
                if row is not None:
                    result[key_public[cache_key]] = self._detach_row(row)
            # A batch read is allowed to overlap a commit, but it must not
            # return a mixture after a complete newer generation is visible.
            if self._read_header()["generation"] == header["generation"]:
                return (result, header["generation"]) if with_generation else result
        raise StorageError(f"fixed table changed while reading: {self.path}")

    def keys(self, key_columns: Sequence[str]) -> list[Any]:
        self._recover_if_needed()
        with self._mapped() as (_handle, mapping):
            pairs: list[tuple[int, Any]] = []
            fields = tuple(field for field in self.fields if field.name in key_columns)
            for slot in range(self.max_records):
                order, row = self._read_record(mapping, slot, fields=fields)
                if row is not None:
                    values = tuple(row[column] for column in key_columns)
                    pairs.append((order, values[0] if len(values) == 1 else values))
        pairs.sort(key=lambda item: item[0])
        return [key for _order, key in pairs]

    def values(self) -> list[dict[str, Any]]:
        return [row for _key, row in self.items()]

    def items(self, key_columns: Sequence[str]) -> list[tuple[Any, dict[str, Any]]]:
        self._recover_if_needed()
        with self._mapped() as (_handle, mapping):
            pairs: list[tuple[int, Any, dict[str, Any]]] = []
            fields = tuple(field for field in self.fields if field.name in key_columns)
            for slot in range(self.max_records):
                order, key_row = self._read_record(mapping, slot, fields=fields)
                if key_row is None:
                    continue
                key_values = tuple(key_row[column] for column in key_columns)
                pairs.append((order, key_values[0] if len(key_values) == 1 else key_values, self._row_for_slot(mapping, slot)))
        pairs.sort(key=lambda item: item[0])
        return [(key, copy.deepcopy(row)) for _order, key, row in pairs]

    def _validate_row(self, row: Mapping[str, Any]) -> None:
        self.definition.validate_rows([row])

    def _validate_patch(self, patch: Mapping[str, Any]) -> None:
        """Validate only the columns touched by an in-place fixed update."""

        field_map = self._fields_by_name
        unknown = set(patch) - set(field_map)
        if unknown:
            raise SchemaError(f"unknown columns in fixed update: {sorted(unknown)!r}")
        for name, value in patch.items():
            column = self._column_definitions[str(name)]
            if column.primary_key and value is None:
                raise SchemaError(f"primary key {name!r} is required")
            if value is not None:
                # Reuse the schema's scalar validator without constructing a
                # complete row (which would force decoding every untouched
                # column for a one-column patch).
                TableDefinition._validate_value(value, column, 1)

    def _patch_record(self, raw: bytes, patch: Mapping[str, Any]) -> tuple[int, bytes]:
        """Patch encoded fields in one slot while retaining untouched bytes."""

        if len(raw) != self.record_size:
            raise StorageError(f"invalid fixed record size: {self.path}")
        state, order, checksum = _FIXED_SLOT_HEADER.unpack(raw[:_FIXED_SLOT_HEADER.size])
        if state != 1:
            if state == 0:
                raise KeyError("fixed slot is empty")
            raise StorageError(f"invalid fixed record state: {self.path}")
        payload = bytearray(raw[_FIXED_SLOT_HEADER.size :])
        if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
            raise StorageError(f"fixed record checksum mismatch: {self.path}")
        field_map = self._fields_by_name
        for name, value in patch.items():
            field = field_map[str(name)]
            encoded = self._encode_field(value, field)
            payload[field.offset : field.offset + field.width] = encoded
        return order, _FIXED_SLOT_HEADER.pack(1, order, zlib.crc32(payload) & 0xFFFFFFFF) + bytes(payload)

    def _patch_mapped_record(
        self,
        mapping: mmap.mmap,
        slot: int,
        patch: Mapping[str, Any],
    ) -> tuple[int, bytes]:
        """Patch one mapped slot without first materialising a bytes slice."""

        offset = self._slot_offset(slot)
        raw = memoryview(mapping)[offset : offset + self.record_size]
        try:
            return self._patch_record(raw, patch)
        finally:
            raw.release()

    def update(self, key_columns: Sequence[str], key: tuple[Any, ...], patch: Mapping[str, Any], *, upsert: bool = False) -> int:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            with self._mapped() as (_handle, mapping):
                index: Optional[dict[tuple[Any, ...], tuple[int, ...]]] = None
                if self._is_primary_key(key_columns):
                    try:
                        slot = self._primary_slot(
                            key,
                            header["generation"],
                            mapping=mapping,
                        )
                    except KeyError:
                        positions = ()
                    else:
                        positions = (slot,)
                else:
                    index = self._key_index_locked(mapping, header["generation"], key_columns)
                    positions = index.get(key, ())
                if not positions:
                    if not upsert:
                        raise KeyError(key[0] if len(key) == 1 else key)
                    row = dict(patch)
                    for column, value in zip(key_columns, key):
                        row.setdefault(column, value)
                    self._validate_row(row)
                    slot = self._free_slot(mapping, header["row_count"])
                    order = self._order_for_generation(header["generation"] + 1)
                    record = self._encode_record(row, order)
                    row_count = header["row_count"] + 1
                    key_unchanged = False
                elif len(positions) != 1:
                    raise ValueError(f"key {key!r} is not unique in fixed table {self.path}")
                else:
                    slot = positions[0]
                    row_count = header["row_count"]
                    patch_values = dict(patch)
                    key_unchanged = not set(patch_values).intersection(key_columns)
                    if key_unchanged:
                        self._validate_patch(patch_values)
                        _order, record = self._patch_mapped_record(mapping, slot, patch_values)
                    else:
                        order, current = self._read_record(mapping, slot)
                        assert current is not None
                        updated = dict(current)
                        updated.update(patch_values)
                        self._validate_row(updated)
                        updated_key = tuple(updated.get(column) for column in key_columns)
                        if updated_key != key:
                            if index is None:
                                index = self._key_index_locked(mapping, header["generation"], key_columns)
                            conflicting = index.get(updated_key, ())
                            if any(candidate != slot for candidate in conflicting):
                                raise SchemaError(f"duplicate primary key {updated_key!r}")
                        record = self._encode_record(updated, order)
                        key_unchanged = updated_key == key
            changed_columns = {str(column) for column in patch}
            with self._key_cache_guard.read():
                preserve_key_cache = bool(positions) and key_unchanged and all(
                    not changed_columns.intersection(cache_key)
                    for cache_key in self._key_cache
                )
            self._commit_locked(
                [(slot, record)],
                row_count,
                header=header,
                preserve_key_cache=preserve_key_cache,
                persisted_index_changed_columns=changed_columns if positions and key_unchanged else None,
            )
            return 1

    def insert_many(self, rows: Sequence[Mapping[str, Any]]) -> int:
        if not rows:
            return 0
        key_columns = self.definition.primary_keys
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            if header["row_count"] + len(rows) > self.max_records:
                raise SchemaError(f"table {self.definition.eng_name!r} reached fixed storage capacity {self.max_records}")
            prepared = [dict(row) for row in rows]
            for row in prepared:
                self._validate_row(row)
            with self._mapped() as (_handle, mapping):
                index = self._key_index_locked(
                    mapping,
                    header["generation"],
                    key_columns,
                    mark_active=False,
                )
                used = set(index)
                free_slots: list[int] = []
                preferred = max(0, min(header["row_count"], self.max_records - 1)) if self.max_records else 0
                for start, stop in ((preferred, self.max_records), (0, preferred)):
                    for slot in range(start, stop):
                        if mapping[self._slot_offset(slot)] == 0:
                            free_slots.append(slot)
                            if len(free_slots) == len(prepared):
                                break
                    if len(free_slots) == len(prepared):
                        break
                if len(free_slots) != len(prepared):
                    raise SchemaError(f"table {self.definition.eng_name!r} has no free fixed slots")
                entries: list[tuple[int, bytes]] = []
                order = self._order_for_generation(header["generation"] + 1)
                with self._key_cache_guard.read():
                    cached_key_columns = tuple(self._key_cache)
                additions_by_columns: dict[tuple[str, ...], list[tuple[tuple[Any, ...], int]]] = {}
                cache_additions_safe = True
                for cached_columns in cached_key_columns:
                    additions_by_columns[cached_columns] = []
                for position, (slot, row) in enumerate(zip(free_slots, prepared)):
                    key = tuple(row[column] for column in key_columns)
                    if key in used:
                        raise SchemaError(f"duplicate primary key {key!r}")
                    used.add(key)
                    entries.append((slot, self._encode_record(row, order + position)))
                    for cached_columns in cached_key_columns:
                        cached_key = tuple(row[column] for column in cached_columns)
                        try:
                            hash(cached_key)
                        except TypeError:
                            cache_additions_safe = False
                        else:
                            additions_by_columns[cached_columns].append((cached_key, slot))
                if not cache_additions_safe:
                    additions_by_columns = {}
            self._commit_locked(
                entries,
                header["row_count"] + len(entries),
                header=header,
                preserve_key_cache=cache_additions_safe,
                key_cache_additions=additions_by_columns if cache_additions_safe else None,
                persisted_index_operation="add",
            )
            return len(entries)

    def set(self, key_columns: Sequence[str], key: tuple[Any, ...], row: Mapping[str, Any], *, upsert: bool = False) -> int:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            with self._mapped() as (_handle, mapping):
                index: Optional[dict[tuple[Any, ...], tuple[int, ...]]] = None
                if self._is_primary_key(key_columns):
                    try:
                        slot = self._primary_slot(
                            key,
                            header["generation"],
                            mapping=mapping,
                        )
                    except KeyError:
                        positions = ()
                    else:
                        positions = (slot,)
                else:
                    index = self._key_index_locked(mapping, header["generation"], key_columns)
                    positions = index.get(key, ())
                if not positions:
                    if not upsert:
                        raise KeyError(key[0] if len(key) == 1 else key)
                    slot = self._free_slot(mapping, header["row_count"])
                    order = self._order_for_generation(header["generation"] + 1)
                    row_count = header["row_count"] + 1
                elif len(positions) != 1:
                    raise ValueError(f"key {key!r} is not unique in fixed table {self.path}")
                else:
                    slot = positions[0]
                    order, _current = self._read_record(mapping, slot)
                    row_count = header["row_count"]
            self._validate_row(row)
            replacement_key = tuple(row.get(column) for column in key_columns)
            if positions and replacement_key != key:
                if index is None:
                    with self._mapped() as (_handle, mapping):
                        index = self._key_index_locked(mapping, header["generation"], key_columns)
                conflicting = index.get(replacement_key, ())
                if any(candidate != slot for candidate in conflicting):
                    raise SchemaError(f"duplicate primary key {replacement_key!r}")
            record = self._encode_record(row, order)
            key_unchanged = replacement_key == key
            self._commit_locked(
                [(slot, record)],
                row_count,
                header=header,
                preserve_key_cache=bool(positions) and key_unchanged,
                persisted_index_changed_columns=(set(self._fields_by_name) if positions else None),
            )
            return 1

    def delete(self, key_columns: Sequence[str], key: tuple[Any, ...]) -> int:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            with self._mapped() as (_handle, mapping):
                if self._is_primary_key(key_columns):
                    slot = self._primary_slot(
                        key,
                        header["generation"],
                        mapping=mapping,
                    )
                else:
                    index = self._key_index_locked(mapping, header["generation"], key_columns)
                    slot = self._find_slot(key_columns, key, index)
            self._commit_locked(
                [(slot, bytes(self.record_size))],
                header["row_count"] - 1,
                header=header,
                preserve_key_cache=True,
                key_cache_removals=(slot,),
                persisted_index_operation="remove",
            )
            return 1

    def update_many(self, key_columns: Sequence[str], updates: Sequence[tuple[tuple[Any, ...], Mapping[str, Any]]]) -> int:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            entries: list[tuple[int, bytes]] = []
            seen: set[tuple[Any, ...]] = set()
            primary_keys = self.definition.primary_keys
            changed_columns: set[str] = set()
            with self._mapped() as (_handle, mapping):
                index = self._key_index_locked(mapping, header["generation"], key_columns)
                primary_index = self._key_index_locked(mapping, header["generation"], primary_keys)
                used_primary = set(primary_index)
                for key, patch in updates:
                    if key in seen:
                        raise ValueError(f"duplicate key in update_many: {key!r}")
                    seen.add(key)
                    slot = self._find_slot(key_columns, key, index)
                    patch_values = dict(patch)
                    changed_columns.update(str(column) for column in patch_values)
                    if not set(patch_values).intersection(primary_keys):
                        self._validate_patch(patch_values)
                        _order, record = self._patch_mapped_record(mapping, slot, patch_values)
                        entries.append((slot, record))
                        continue
                    order, current = self._read_record(mapping, slot)
                    assert current is not None
                    updated = dict(current)
                    updated.update(patch_values)
                    self._validate_row(updated)
                    updated_primary = tuple(updated.get(column) for column in primary_keys)
                    current_primary = tuple(current.get(column) for column in primary_keys)
                    if updated_primary != current_primary:
                        conflicting = primary_index.get(updated_primary, ())
                        if any(candidate != slot for candidate in conflicting):
                            raise SchemaError(f"duplicate primary key {updated_primary!r}")
                        used_primary.discard(current_primary)
                        if updated_primary in used_primary:
                            raise SchemaError(f"duplicate primary key {updated_primary!r}")
                        used_primary.add(updated_primary)
                    entries.append((slot, self._encode_record(updated, order)))
            with self._key_cache_guard.read():
                preserve_key_cache = all(
                    not changed_columns.intersection(cache_key)
                    for cache_key in self._key_cache
                )
            self._commit_locked(
                entries,
                header["row_count"],
                header=header,
                preserve_key_cache=preserve_key_cache,
                persisted_index_changed_columns=changed_columns,
            )
            return len(entries)

    def delete_many(self, key_columns: Sequence[str], keys: Sequence[tuple[Any, ...]]) -> int:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            with self._mapped() as (_handle, mapping):
                index = self._key_index_locked(mapping, header["generation"], key_columns)
                slots: list[int] = []
                seen: set[tuple[Any, ...]] = set()
                for key in keys:
                    if key in seen:
                        raise ValueError(f"duplicate key in delete_many: {key!r}")
                    seen.add(key)
                    slots.append(self._find_slot(key_columns, key, index))
            self._commit_locked(
                [(slot, bytes(self.record_size)) for slot in slots],
                header["row_count"] - len(slots),
                header=header,
                preserve_key_cache=True,
                key_cache_removals=slots,
                persisted_index_operation="remove",
            )
            return len(slots)

    def replace_snapshot_locked(self, rows: Sequence[Mapping[str, Any]], generation: int) -> None:
        """Rewrite a fixed table through the binary WAL, in place.

        Fixed tables do not need an atomic file replacement: the WAL is made
        durable before any slot is changed, and the generation header is
        published last.  Keeping the file identity stable also lets readers
        reuse a memory mapping and avoids Windows ``os.replace`` contention.
        """

        if len(rows) > self.max_records:
            raise SchemaError(f"table {self.definition.eng_name!r} reached fixed storage capacity {self.max_records}")
        self.definition.validate_rows(rows)
        entries = [
            (slot, self._encode_record(rows[slot], self._order_for_generation(generation, slot)))
            if slot < len(rows)
            else (slot, bytes(self.record_size))
            for slot in range(self.max_records)
        ]
        frame_size = len(_FIXED_WAL_HEADER.pack(_FIXED_WAL_MAGIC, 0, 0)) + len(_FIXED_WAL_TRAILER.pack(_FIXED_WAL_END_MAGIC, 0))
        frame_size += _FIXED_WAL_PAYLOAD_HEADER.size + sum(_FIXED_WAL_ENTRY_HEADER.size + len(record) for _slot, record in entries)
        if frame_size > _MAX_WAL_RECORD:
            raise StorageError(
                f"fixed snapshot rewrite is too large for one WAL frame ({frame_size} bytes > {_MAX_WAL_RECORD})"
            )
        try:
            self._commit_locked(entries, len(rows), generation=generation)
            self.active = True
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(f"cannot replace fixed table {self.path}: {exc}") from exc

    def snapshot_locked(self) -> _Snapshot:
        self._recover_locked()
        return self._snapshot_from_file()

    def _snapshot_from_file(self) -> _Snapshot:
        header = self._read_header()
        with self._cache_guard:
            if self._snapshot_cache is not None and self._snapshot_cache.generation == header["generation"]:
                return self._snapshot_cache
        rows_with_order: list[tuple[int, dict[str, Any]]] = []
        with self._mapped() as (_handle, mapping):
            for slot in range(self.max_records):
                order, row = self._read_record(mapping, slot)
                if row is not None:
                    rows_with_order.append((order, row))
        rows_with_order.sort(key=lambda item: item[0])
        rows = [row for _order, row in rows_with_order]
        indexes, primary_index = _build_snapshot_indexes(rows, self.definition)
        signature = _file_signature(self.path) + _file_signature(self.wal_path) + (header["generation"], header["generation"])
        snapshot = _Snapshot(rows, list(self.definition.column_names), header["generation"], signature, indexes, primary_index)
        with self._cache_guard:
            self._snapshot_cache = snapshot
        return snapshot

    def snapshot(self) -> _Snapshot:
        self._recover_if_needed()
        return self._snapshot_from_file()

    def row_count(self) -> int:
        """Return the published row count without decoding the row area."""

        self._recover_if_needed()
        return int(self._read_header()["row_count"])


class QueryResult(list):
    """List-like result returned by SQL statements.

    ``SELECT`` results are directly iterable and indexable.  For all statement
    types, ``rowcount`` reports affected rows and ``lastrowid`` reports the
    inserted row index when applicable.
    """

    def __init__(self, rows: Iterable[Mapping[str, Any]] = (), *, rowcount: Optional[int] = None, lastrowid: Optional[int] = None) -> None:
        super().__init__(copy.deepcopy(list(rows)))
        self.rowcount = len(self) if rowcount is None else rowcount
        self.lastrowid = lastrowid

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self)


class Table(MutableSequence[dict[str, Any]]):
    """A relation persisted in one memory-mapped table file.

    ``Table`` implements the common mutable-list operations in addition to
    relation-oriented methods such as :meth:`select`, :meth:`update`,
    :meth:`delete` and :meth:`execute`.
    """

    def __init__(
        self,
        path: Union[str, os.PathLike[str]],
        *,
        name: Optional[str] = None,
        definition: Optional[TableDefinition] = None,
        lock_timeout: Optional[float] = 30.0,
        fixed_max_records: int = _FIXED_DEFAULT_MAX_RECORDS,
        fixed_text_bytes: int = _FIXED_DEFAULT_TEXT_BYTES,
        fixed_object_bytes: int = _FIXED_DEFAULT_OBJECT_BYTES,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        if self.path.suffix == "":
            self.path = self.path.with_suffix(".table")
        self.name = _validate_identifier(name or self.path.stem, label="table name")
        if definition is None and self.path.exists():
            try:
                with self.path.open("rb") as handle:
                    fixed_magic = handle.read(len(_FIXED_MAGIC))
            except PermissionError:
                # A snapshot table may be atomically replaced by another
                # process while this object is being constructed.  The normal
                # table lock/read path will retry; do not turn contention into
                # a format error before the lock is acquired.
                fixed_magic = b""
            except OSError as exc:
                raise StorageError(f"cannot inspect table {self.path}: {exc}") from exc
            if fixed_magic == _FIXED_MAGIC:
                definition = _load_fixed_definition(self.path, self.name)
        if definition is not None and definition.eng_name != self.name:
            raise ValueError(
                f"table definition {definition.eng_name!r} does not match table {self.name!r}"
            )
        self.definition = definition
        if lock_timeout is not None and lock_timeout <= 0:
            raise ValueError("lock_timeout must be positive or None")
        self.lock_timeout = lock_timeout
        self.fixed_max_records = int(fixed_max_records)
        self.fixed_text_bytes = int(fixed_text_bytes)
        self.fixed_object_bytes = int(fixed_object_bytes)
        if self.fixed_max_records < 1 or self.fixed_text_bytes < 1 or self.fixed_object_bytes < 1024:
            raise ValueError("fixed storage settings are too small")
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.wal_path = self.path.with_suffix(self.path.suffix + ".wal")
        self._fixed_store = (
            _FixedRecordStore(self.path, definition, self.lock_path, self.lock_timeout,
                              max_records=self.fixed_max_records,
                              text_bytes=self.fixed_text_bytes,
                              object_bytes=self.fixed_object_bytes)
            if definition is not None
            else None
        )
        self._cache: Optional[_Snapshot] = None
        self._cache_guard = threading.RLock()
        # The view keeps a strong reference back to this Table for temporary
        # expressions such as ``FileMappedDatabase(root)[name].kv``.  Store
        # only a weak reference on the Table side to avoid a view/table cycle
        # while preserving that lifetime guarantee.
        self._kv_view: Optional[weakref.ReferenceType[TableKeyValue]] = None
        self._ensure_exists()

    def _ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.lock_path, timeout=self.lock_timeout):
            if self._fixed_store is not None:
                self._fixed_store.ensure_exists_locked()
                if self._fixed_store.active:
                    return
            if not self.path.exists():
                self._write_snapshot_locked([], [], 0)

    def _read_snapshot_locked(self) -> _Snapshot:
        """Read the current immutable snapshot.

        The method name is retained for the write path, but readers no longer
        need to hold the table's exclusive file lock.  Writers publish with
        ``os.replace``; the optimistic read below retries if a replacement is
        observed while the file is being mapped.
        """

        if self._fixed_store is not None and self._fixed_store.active:
            return self._fixed_store.snapshot_locked()
        if not self.path.exists():
            self._write_snapshot_locked([], [], 0)
        self._recover_wal_tail_locked()
        return self._read_snapshot()

    def _read_snapshot(self) -> _Snapshot:
        if self._fixed_store is not None and self._fixed_store.active:
            return self._fixed_store.snapshot()
        if not self.path.exists():
            # Creation is always protected by the inter-process lock.  The
            # constructor normally creates the file, but this also handles a
            # table directory that was removed and recreated externally.
            with _locked(self.lock_path, timeout=self.lock_timeout):
                if not self.path.exists():
                    self._write_snapshot_locked([], [], 0)
        last_error: Optional[Exception] = None
        for _attempt in range(4):
            try:
                base_signature = _file_signature(self.path)
                wal_signature = _file_signature(self.wal_path)
                with self._cache_guard:
                    cached = self._cache
                if cached is not None and cached.cache_key[:8] == base_signature + wal_signature:
                    return cached
                if (
                    cached is not None
                    and cached.cache_key[:4] == base_signature
                    and cached.cache_key[7] == wal_signature[3]
                    and wal_signature[1] >= cached.cache_key[5]
                ):
                    incremental_records, _incomplete, _valid_end = self._read_wal_records(
                        start_offset=cached.cache_key[5]
                    )
                    after_base_signature = _file_signature(self.path)
                    after_wal_signature = _file_signature(self.wal_path)
                    if after_base_signature != base_signature or after_wal_signature != wal_signature:
                        continue
                    incremental_rows = list(cached.rows)
                    incremental_columns = list(cached.columns)
                    incremental_rows, incremental_columns, current_generation = self._replay_wal(
                        incremental_rows,
                        incremental_columns,
                        cached.generation,
                        incremental_records,
                    )
                    if self.definition is not None:
                        try:
                            self.definition.validate_rows(incremental_rows)
                        except SchemaError as exc:
                            raise StorageError(f"table {self.name!r} does not match its definition: {exc}") from exc
                    indexes, primary_index = _build_snapshot_indexes(incremental_rows, self.definition)
                    snapshot = _Snapshot(
                        incremental_rows,
                        incremental_columns,
                        current_generation,
                        base_signature + wal_signature + (cached.cache_key[8], current_generation),
                        indexes,
                        primary_index,
                    )
                    with self._cache_guard:
                        self._cache = snapshot
                    return snapshot
                stat = self.path.stat()
                with self.path.open("rb") as handle:
                    if stat.st_size < _HEADER.size:
                        raise StorageError(f"table file is truncated: {self.path}")
                    with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                        magic, payload_size, generation, checksum = _HEADER.unpack(mapped[:_HEADER.size])
                        if magic != _MAGIC:
                            raise StorageError(f"invalid table header: {self.path}")
                        if payload_size > _MAX_PAYLOAD or _HEADER.size + payload_size > stat.st_size:
                            raise StorageError(f"invalid payload length in {self.path}")
                        payload = mapped[_HEADER.size : _HEADER.size + payload_size]
                records, _incomplete, _valid_end = self._read_wal_records()
                after_base_signature = _file_signature(self.path)
                after_wal_signature = _file_signature(self.wal_path)
                if after_base_signature != base_signature or after_wal_signature != wal_signature:
                    continue
                if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                    raise StorageError(f"checksum mismatch in {self.path}")
                value = json.loads(payload.decode("utf-8"))
                rows = value.get("rows")
                columns = value.get("columns", [])
                if value.get("version") != 1 or not isinstance(rows, list) or not isinstance(columns, list):
                    raise StorageError(f"invalid table payload: {self.path}")
                normalised_rows: list[dict[str, Any]] = []
                for row in rows:
                    if not isinstance(row, Mapping):
                        raise StorageError(f"table row is not an object: {self.path}")
                    normalised_rows.append(dict(row))
                normalised_columns = [_validate_identifier(str(column), label="column name") for column in columns]
                normalised_rows, normalised_columns, current_generation = self._replay_wal(
                    normalised_rows,
                    normalised_columns,
                    int(generation),
                    records,
                )
                if self.definition is not None:
                    try:
                        self.definition.validate_rows(normalised_rows)
                    except SchemaError as exc:
                        raise StorageError(f"table {self.name!r} does not match its definition: {exc}") from exc
                indexes, primary_index = _build_snapshot_indexes(normalised_rows, self.definition)
                snapshot = _Snapshot(
                    normalised_rows,
                    normalised_columns,
                    current_generation,
                    base_signature + wal_signature + (int(generation), current_generation),
                    indexes,
                    primary_index,
                )
                with self._cache_guard:
                    self._cache = snapshot
                return snapshot
            except StorageError as exc:
                last_error = exc
                break
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(0.001 * (_attempt + 1))
        if isinstance(last_error, StorageError):
            raise last_error
        raise StorageError(f"cannot read table {self.path}: {last_error}") from last_error

    def _read_wal_records(self, *, start_offset: int = 0) -> tuple[list[dict[str, Any]], bool, int]:
        """Read complete WAL frames and report a possibly incomplete tail.

        A short final frame is safe to ignore while a writer is still flushing
        it, and is also the only condition that may be repaired after a crash.
        A complete frame with a bad CRC is reported as corruption instead.
        """

        if not self.wal_path.exists():
            return [], False, 0
        records: list[dict[str, Any]] = []
        valid_end = start_offset
        try:
            with self.wal_path.open("rb") as handle:
                if start_offset:
                    handle.seek(start_offset)
                while True:
                    header = handle.read(_WAL_HEADER.size)
                    if not header:
                        return records, False, valid_end
                    if len(header) < _WAL_HEADER.size:
                        return records, True, valid_end
                    magic, payload_size, checksum = _WAL_HEADER.unpack(header)
                    if magic != _WAL_MAGIC:
                        raise StorageError(f"invalid WAL header: {self.wal_path}")
                    if payload_size > _MAX_WAL_RECORD:
                        raise StorageError(f"invalid WAL payload length: {self.wal_path}")
                    payload = handle.read(payload_size)
                    if len(payload) < payload_size:
                        return records, True, valid_end
                    trailer = handle.read(_WAL_TRAILER.size)
                    if len(trailer) < _WAL_TRAILER.size:
                        return records, True, valid_end
                    end_magic, end_length = _WAL_TRAILER.unpack(trailer)
                    if end_magic != _WAL_END_MAGIC or end_length != payload_size:
                        raise StorageError(f"invalid WAL trailer: {self.wal_path}")
                    if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                        raise StorageError(f"WAL checksum mismatch: {self.wal_path}")
                    try:
                        record = json.loads(payload.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise StorageError(f"invalid WAL payload: {self.wal_path}") from exc
                    if not isinstance(record, dict) or record.get("version") != 1:
                        raise StorageError(f"invalid WAL record: {self.wal_path}")
                    if not isinstance(record.get("seq"), int) or isinstance(record.get("seq"), bool):
                        raise StorageError(f"invalid WAL sequence: {self.wal_path}")
                    records.append(record)
                    valid_end += _WAL_HEADER.size + payload_size + _WAL_TRAILER.size
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(f"cannot read WAL {self.wal_path}: {exc}") from exc

    def _replay_wal(
        self,
        rows: list[dict[str, Any]],
        columns: list[str],
        generation: int,
        records: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str], int]:
        """Apply WAL records newer than the base snapshot generation."""

        current = generation
        replay_indexes: dict[str, Optional[dict[Any, tuple[int, ...]]]] = {}
        for record in records:
            sequence = int(record["seq"])
            if sequence <= generation:
                continue
            operation = record.get("op")
            if operation == "checkpoint":
                if sequence < current:
                    raise StorageError(f"WAL sequence moved backwards: {self.wal_path}")
                raw_rows = record.get("rows")
                raw_columns = record.get("columns")
                if not isinstance(raw_rows, list) or not isinstance(raw_columns, list):
                    raise StorageError(f"invalid WAL checkpoint: {self.wal_path}")
                rows = []
                for row in raw_rows:
                    if not isinstance(row, Mapping):
                        raise StorageError(f"invalid WAL checkpoint row: {self.wal_path}")
                    rows.append(dict(row))
                columns = [_validate_identifier(str(column), label="column name") for column in raw_columns]
                replay_indexes.clear()
                current = sequence
                continue
            if sequence != current + 1:
                raise StorageError(
                    f"WAL sequence gap in {self.wal_path}: expected {current + 1}, got {sequence}"
                )
            if operation == "append":
                raw_rows = record.get("rows")
                if not isinstance(raw_rows, list):
                    raise StorageError(f"invalid WAL append: {self.wal_path}")
                for row in raw_rows:
                    if not isinstance(row, Mapping):
                        raise StorageError(f"invalid WAL append row: {self.wal_path}")
                    rows.append(dict(row))
                columns = self._columns_after(rows, columns, (key for row in raw_rows for key in row))
                replay_indexes.clear()
            elif operation in {"update_key", "delete_key", "replace_key", "update_keys", "delete_keys"}:
                raw_columns = record.get("key_columns")
                if not isinstance(raw_columns, list) or not raw_columns:
                    raise StorageError(f"invalid WAL key columns: {self.wal_path}")
                try:
                    key_columns = tuple(_validate_identifier(str(column), label="key column") for column in raw_columns)
                except ValueError as exc:
                    raise StorageError(f"invalid WAL key columns: {self.wal_path}") from exc
                key_index = _build_key_positions(rows, key_columns)
                if key_index is None:
                    raise StorageError(f"WAL key column is missing or unhashable: {self.wal_path}")

                def key_from_record(raw_key: Any) -> tuple[Any, ...]:
                    if not isinstance(raw_key, list) or len(raw_key) != len(key_columns):
                        raise StorageError(f"invalid WAL key: {self.wal_path}")
                    try:
                        key = tuple(raw_key)
                        hash(key)
                    except TypeError as exc:
                        raise StorageError(f"invalid WAL key: {self.wal_path}") from exc
                    return key

                def unique_position(raw_key: Any) -> int:
                    key = key_from_record(raw_key)
                    positions = key_index.get(key, ())
                    if len(positions) != 1:
                        raise StorageError(f"WAL key target is not unique: {self.wal_path}")
                    return positions[0]

                if operation in {"update_key", "delete_key", "replace_key"}:
                    position = unique_position(record.get("key"))
                    if operation == "delete_key":
                        del rows[position]
                    else:
                        values = record.get("values")
                        if not isinstance(values, Mapping):
                            raise StorageError(f"invalid WAL key values: {self.wal_path}")
                        if any(column in values for column in key_columns):
                            current_key = tuple(rows[position].get(column) for column in key_columns)
                            proposed_key = tuple(dict(values).get(column, rows[position].get(column)) for column in key_columns)
                            if proposed_key != current_key:
                                raise StorageError(f"WAL key mutation is not supported: {self.wal_path}")
                        if operation == "replace_key":
                            rows[position] = dict(values)
                        else:
                            updated = dict(rows[position])
                            updated.update(dict(values))
                            rows[position] = updated
                        columns = self._columns_after(rows, columns, values.keys())
                    replay_indexes.clear()
                    current = sequence
                    continue

                if operation == "update_keys":
                    entries = record.get("entries")
                    if not isinstance(entries, list):
                        raise StorageError(f"invalid WAL key update entries: {self.wal_path}")
                    for entry in entries:
                        if not isinstance(entry, Mapping):
                            raise StorageError(f"invalid WAL key update entry: {self.wal_path}")
                        position = unique_position(entry.get("key"))
                        values = entry.get("values")
                        if not isinstance(values, Mapping):
                            raise StorageError(f"invalid WAL key values: {self.wal_path}")
                        if any(column in values for column in key_columns):
                            current_key = tuple(rows[position].get(column) for column in key_columns)
                            proposed_key = tuple(dict(values).get(column, rows[position].get(column)) for column in key_columns)
                            if proposed_key != current_key:
                                raise StorageError(f"WAL key mutation is not supported: {self.wal_path}")
                        updated = dict(rows[position])
                        updated.update(dict(values))
                        rows[position] = updated
                        columns = self._columns_after(rows, columns, values.keys())
                    replay_indexes.clear()
                    current = sequence
                    continue

                raw_keys = record.get("keys")
                if not isinstance(raw_keys, list):
                    raise StorageError(f"invalid WAL key deletion entries: {self.wal_path}")
                positions = {unique_position(raw_key) for raw_key in raw_keys}
                if len(positions) != len(raw_keys):
                    raise StorageError(f"duplicate WAL key deletion: {self.wal_path}")
                rows[:] = [row for position, row in enumerate(rows) if position not in positions]
                replay_indexes.clear()
                current = sequence
                continue
            elif operation in {"update_pk", "delete_pk", "update_where", "delete_where"}:
                if operation in {"update_where", "delete_where"}:
                    raw_where = record.get("where")
                    if not isinstance(raw_where, Mapping):
                        raise StorageError(f"invalid WAL WHERE clause: {self.wal_path}")
                    predicate = _compile_where(raw_where)
                    raw_limit = record.get("limit")
                    if raw_limit is not None and (not isinstance(raw_limit, int) or raw_limit < 0):
                        raise StorageError(f"invalid WAL limit: {self.wal_path}")
                    candidate_positions: Optional[tuple[int, ...]] = None
                    if len(raw_where) == 1:
                        field, value = next(iter(raw_where.items()))
                        field = str(field)
                        if field not in replay_indexes:
                            replay_indexes[field] = _build_column_index(rows, field)
                        indexed = replay_indexes[field]
                        if indexed is not None:
                            try:
                                candidate_positions = indexed.get(value, ())
                            except TypeError:
                                candidate_positions = None
                    changed = 0
                    if operation == "update_where":
                        values = record.get("values")
                        if not isinstance(values, Mapping):
                            raise StorageError(f"invalid WAL update: {self.wal_path}")
                        source = enumerate(rows) if candidate_positions is None else ((position, rows[position]) for position in candidate_positions)
                        for position, row in source:
                            if predicate(row) and (raw_limit is None or changed < raw_limit):
                                updated = dict(row)
                                updated.update(dict(values))
                                rows[position] = updated
                                changed += 1
                        columns = self._columns_after(rows, columns, values.keys())
                        for field in values:
                            replay_indexes.pop(str(field), None)
                    else:
                        kept: list[dict[str, Any]] = []
                        selected = None if candidate_positions is None else set(candidate_positions)
                        for position, row in enumerate(rows):
                            if (selected is None or position in selected) and predicate(row) and (raw_limit is None or changed < raw_limit):
                                changed += 1
                            else:
                                kept.append(row)
                        rows = kept
                        replay_indexes.clear()
                    current = sequence
                    continue
                if self.definition is None or not self.definition.primary_keys:
                    raise StorageError(f"primary-key WAL requires a table definition: {self.name}")
                raw_key = record.get("key")
                if not isinstance(raw_key, list) or len(raw_key) != len(self.definition.primary_keys):
                    raise StorageError(f"invalid WAL primary key: {self.wal_path}")
                target = tuple(raw_key)
                matches = [
                    index
                    for index, row in enumerate(rows)
                    if tuple(row.get(column) for column in self.definition.primary_keys) == target
                ]
                if len(matches) != 1:
                    raise StorageError(f"WAL primary key target is not unique: {self.wal_path}")
                position = matches[0]
                if operation == "update_pk":
                    values = record.get("values")
                    if not isinstance(values, Mapping):
                        raise StorageError(f"invalid WAL update: {self.wal_path}")
                    updated = dict(rows[position])
                    updated.update(dict(values))
                    rows[position] = updated
                    columns = self._columns_after(rows, columns, values.keys())
                    replay_indexes.clear()
                else:
                    del rows[position]
                    replay_indexes.clear()
            else:
                raise StorageError(f"unsupported WAL operation {operation!r}: {self.wal_path}")
            current = sequence
        return rows, columns, current

    def _recover_wal_tail_locked(self) -> None:
        if not self.wal_path.exists():
            return
        try:
            file_size = self.wal_path.stat().st_size
            if file_size >= _WAL_TRAILER.size:
                with self.wal_path.open("rb") as handle:
                    handle.seek(-_WAL_TRAILER.size, os.SEEK_END)
                    trailer = handle.read(_WAL_TRAILER.size)
                end_magic, end_length = _WAL_TRAILER.unpack(trailer)
                if end_magic == _WAL_END_MAGIC and end_length <= _MAX_WAL_RECORD:
                    return
        except (OSError, struct.error) as exc:
            raise StorageError(f"cannot inspect WAL tail {self.wal_path}: {exc}") from exc
        valid_end = 0
        incomplete = False
        try:
            with self.wal_path.open("rb") as handle:
                while True:
                    header = handle.read(_WAL_HEADER.size)
                    if not header:
                        break
                    if len(header) < _WAL_HEADER.size:
                        incomplete = True
                        break
                    magic, payload_size, checksum = _WAL_HEADER.unpack(header)
                    if magic != _WAL_MAGIC or payload_size > _MAX_WAL_RECORD:
                        raise StorageError(f"invalid WAL frame: {self.wal_path}")
                    payload = handle.read(payload_size)
                    if len(payload) < payload_size:
                        incomplete = True
                        break
                    trailer = handle.read(_WAL_TRAILER.size)
                    if len(trailer) < _WAL_TRAILER.size:
                        incomplete = True
                        break
                    end_magic, end_length = _WAL_TRAILER.unpack(trailer)
                    if end_magic != _WAL_END_MAGIC or end_length != payload_size:
                        raise StorageError(f"invalid WAL trailer: {self.wal_path}")
                    if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                        raise StorageError(f"WAL checksum mismatch: {self.wal_path}")
                    valid_end += _WAL_HEADER.size + payload_size + _WAL_TRAILER.size
            if not incomplete:
                return
            with self.wal_path.open("r+b") as handle:
                handle.truncate(valid_end)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageError(f"cannot repair WAL tail {self.wal_path}: {exc}") from exc

    def _append_wal_record_locked(self, record: Mapping[str, Any]) -> None:
        frame = _wal_frame(record)
        self.wal_path.parent.mkdir(parents=True, exist_ok=True)
        self._recover_wal_tail_locked()
        existed = self.wal_path.exists()
        try:
            with self.wal_path.open("ab") as handle:
                handle.write(frame)
                handle.flush()
                os.fsync(handle.fileno())
            if not existed and os.name != "nt":  # pragma: no cover - POSIX durability detail
                directory_fd = os.open(self.wal_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise StorageError(f"cannot append WAL {self.wal_path}: {exc}") from exc

    def _cache_snapshot(
        self,
        rows: list[dict[str, Any]],
        columns: list[str],
        generation: int,
        *,
        base_generation: int,
    ) -> None:
        indexes, primary_index = _build_snapshot_indexes(rows, self.definition)
        snapshot = _Snapshot(
            rows,
            list(columns),
            generation,
            _file_signature(self.path) + _file_signature(self.wal_path) + (base_generation, generation),
            indexes,
            primary_index,
        )
        with self._cache_guard:
            self._cache = snapshot

    def _replace_wal_with_checkpoint_locked(self, record: Mapping[str, Any]) -> None:
        encoded = _wal_frame(record)
        temporary: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb", prefix=f".{self.wal_path.name}.", suffix=".tmp", dir=self.wal_path.parent, delete=False
            ) as handle:
                temporary = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    os.replace(temporary, self.wal_path)
                    temporary = None
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.002)
            if os.name != "nt":  # pragma: no cover - POSIX durability detail
                directory_fd = os.open(self.wal_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise StorageError(f"cannot rotate WAL {self.wal_path}: {exc}") from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _compact_locked(self, rows: list[dict[str, Any]], columns: list[str], generation: int) -> None:
        """Publish a checkpoint before rotating the WAL.

        The checkpoint makes every intermediate combination safe: an old base
        can recover through the new WAL, while a new base can ignore the
        checkpoint at the same generation.  Only after the base is durable is
        the WAL replaced by the checkpoint-only file.
        """

        checkpoint = self._checkpoint_record(rows, columns, generation)
        self._append_wal_record_locked(checkpoint)
        self._write_snapshot_locked(rows, columns, generation)
        self._replace_wal_with_checkpoint_locked(checkpoint)
        self._cache_snapshot(rows, columns, generation, base_generation=generation)

    @staticmethod
    def _checkpoint_record(rows: list[dict[str, Any]], columns: list[str], generation: int) -> dict[str, Any]:
        return {
            "version": 1,
            "seq": generation,
            "op": "checkpoint",
            "columns": list(columns),
            "rows": rows,
        }

    def _write_snapshot_locked(self, rows: list[dict[str, Any]], columns: list[str], generation: int) -> None:
        if self._fixed_store is not None and self._fixed_store.active:
            self._fixed_store.replace_snapshot_locked(rows, generation)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = _snapshot_payload(rows, columns, generation)
        temporary: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile("wb", prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, delete=False) as handle:
                temporary = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    os.replace(temporary, self.path)
                    temporary = None
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.002)
            if os.name != "nt":  # pragma: no cover - POSIX durability detail
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self._cache_snapshot(rows, columns, generation, base_generation=generation)
        except (OSError, TypeError, ValueError) as exc:
            raise StorageError(f"cannot write table {self.path}: {exc}") from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @staticmethod
    def _normalise_row(row: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(row, Mapping):
            raise TypeError("rows must be mapping objects, such as dict")
        # Detach nested JSON values from caller-owned objects.  This lets the
        # post-write cache safely reuse the newly built snapshot without a
        # second deep copy of the entire table.
        result = copy.deepcopy(dict(row))
        for key in result:
            _validate_identifier(str(key), label="column name")
        # Validate snapshot serialization before taking the table lock for a write.
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"row contains a non-JSON value: {exc}") from exc
        return result

    @staticmethod
    def _columns_after(rows: Sequence[Mapping[str, Any]], columns: Sequence[str], new_columns: Iterable[str] = ()) -> list[str]:
        result = list(columns)
        for key in new_columns:
            key = str(key)
            if key not in result:
                result.append(key)
        return result

    @contextmanager
    def _transaction(
        self,
        *,
        copy_rows: bool = True,
    ) -> Iterator[tuple[list[dict[str, Any]], list[str], int, _Snapshot]]:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            snapshot = self._read_snapshot_locked()
            rows = copy.deepcopy(snapshot.rows) if copy_rows else list(snapshot.rows)
            columns = list(snapshot.columns)
            yield rows, columns, snapshot.generation, snapshot

    def _mutate(
        self,
        function: Callable[[list[dict[str, Any]], list[str]], Any],
        *,
        copy_rows: bool = True,
        new_columns: Iterable[str] = (),
    ) -> Any:
        with self._transaction(copy_rows=copy_rows) as (rows, columns, generation, _snapshot):
            result = function(rows, columns)
            columns = self._columns_after(rows, columns, new_columns)
            if self.definition is not None:
                self.definition.validate_rows(rows)
            self._write_snapshot_locked(rows, columns, generation + 1)
            if self.wal_path.exists() and self.wal_path.stat().st_size >= _WAL_COMPACT_BYTES:
                self._compact_locked(rows, columns, generation + 1)
            return result

    def _mutate_with_snapshot(
        self,
        function: Callable[[list[dict[str, Any]], list[str], _Snapshot], Any],
        *,
        copy_rows: bool = True,
        new_columns: Iterable[str] = (),
    ) -> Any:
        """Run a mutation with the exact snapshot held by the write lock."""

        with self._transaction(copy_rows=copy_rows) as (rows, columns, generation, snapshot):
            result = function(rows, columns, snapshot)
            columns = self._columns_after(rows, columns, new_columns)
            if self.definition is not None:
                self.definition.validate_rows(rows)
            self._write_snapshot_locked(rows, columns, generation + 1)
            if self.wal_path.exists() and self.wal_path.stat().st_size >= _WAL_COMPACT_BYTES:
                self._compact_locked(rows, columns, generation + 1)
            return result

    def _mutate_incremental(
        self,
        function: Callable[[list[dict[str, Any]], list[str], _Snapshot], tuple[Any, dict[str, Any]]],
        *,
        copy_rows: bool = False,
    ) -> Any:
        """Commit an eligible mutation through one durable WAL frame."""

        with _locked(self.lock_path, timeout=self.lock_timeout):
            snapshot = self._read_snapshot_locked()
            rows = copy.deepcopy(snapshot.rows) if copy_rows else list(snapshot.rows)
            columns = list(snapshot.columns)
            result, record = function(rows, columns, snapshot)
            record = dict(record)
            record_columns = record.pop("columns", ())
            columns = self._columns_after(rows, columns, record_columns)
            if self.definition is not None:
                self.definition.validate_rows(rows)
            generation = snapshot.generation + 1
            if self._fixed_store is not None and self._fixed_store.active:
                self._fixed_store.replace_snapshot_locked(rows, generation)
                return result
            record.update({"version": 1, "seq": generation})
            try:
                self._append_wal_record_locked(record)
            except _WalRecordTooLarge:
                # A large batch is still committed atomically, but serialising
                # it as a full snapshot avoids imposing an arbitrary row-size
                # limit on the public API.
                self._write_snapshot_locked(rows, columns, generation)
            else:
                wal_size = self.wal_path.stat().st_size
                if wal_size >= _WAL_COMPACT_BYTES:
                    self._compact_locked(rows, columns, generation)
                else:
                    self._cache_snapshot(
                        rows,
                        columns,
                        generation,
                        base_generation=snapshot.generation,
                    )
            return result

    def _read_rows(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._read_snapshot().rows)

    @property
    def columns(self) -> list[str]:
        """Return the table's known columns in first-seen order."""

        return self._read_snapshot_columns()

    @property
    def schema(self) -> Optional[TableDefinition]:
        """Return the loaded JSON definition, if this table has one."""

        return self.definition

    @staticmethod
    def _candidate_positions(
        snapshot: _Snapshot,
        where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]],
    ) -> Optional[list[int]]:
        """Return candidate row positions for equality predicates.

        A missing index returns ``None`` so callers retain the original full
        scan behaviour.  Indexed candidates are still passed through the full
        predicate, which keeps combined conditions correct.
        """

        if not isinstance(where, Mapping) or not where:
            return None
        candidate_sets: list[set[int]] = []
        for field, value in where.items():
            index = snapshot.indexes.get(str(field))
            if index is None:
                continue
            try:
                candidate_sets.append(set(index.get(value, ())))
            except TypeError:
                return None
        if not candidate_sets:
            return None
        positions = candidate_sets[0]
        for candidate in candidate_sets[1:]:
            positions.intersection_update(candidate)
        return sorted(positions)

    def _primary_key_position(self, key: Any) -> int:
        """Return the unique primary-key position or raise ``KeyError``."""

        snapshot = self._read_snapshot()
        return self._primary_key_position_in_snapshot(snapshot, key)

    def _primary_key_position_in_snapshot(self, snapshot: _Snapshot, key: Any) -> int:
        """Return a primary-key position from one already-read snapshot."""

        if self.definition is not None and len(self.definition.primary_keys) > 1:
            lookup = tuple(key) if isinstance(key, (tuple, list)) else (key,)
        else:
            lookup = (key,)
        positions = snapshot.primary_index.get(lookup)
        if not positions or len(positions) != 1:
            raise KeyError(key)
        return positions[0]

    def get_by_pk(self, key: Any) -> dict[str, Any]:
        """Read one row by the declared primary key without a table scan."""

        if self._fixed_store is not None and self._fixed_store.active:
            return self.kv.require(key)
        snapshot = self._read_snapshot()
        return copy.deepcopy(snapshot.rows[self._primary_key_position_in_snapshot(snapshot, key)])

    def key_value(self, key_column: Optional[str] = None) -> "TableKeyValue":
        """Return a dictionary-like, indexed view of this table.

        With no argument the declared primary key is used.  Tables without a
        JSON definition default to the conventional ``id`` column.  Passing
        a column name selects a single-column key and is useful for an
        application-defined unique identifier.  The view never exposes the
        cached row dictionaries directly: reads are detached copies and all
        writes use the table lock and durable WAL path.
        """

        if key_column is not None:
            return TableKeyValue(self, key_column=key_column)
        with self._cache_guard:
            view = self._kv_view() if self._kv_view is not None else None
            if view is None:
                view = TableKeyValue(self)
                self._kv_view = weakref.ref(view)
            return view

    def transaction(self) -> "TableWriteBatch":
        """Return a durable write batch for repeated fixed-table updates.

        Calls made through the returned object are queued in memory and
        committed when the context exits.  Updates or deletes are resolved in
        one fixed-table WAL frame, so the batch pays one WAL ``fsync``
        regardless of its row count.  The table mapping follows the bounded
        checkpoint policy as ordinary writes, while this context keeps the
        complete group in one atomic generation.

        A batch contains one operation family (updates, deletes, or inserts),
        which makes its commit atomic and keeps the durability contract clear.
        """

        return TableWriteBatch(self)

    @property
    def kv(self) -> "TableKeyValue":
        """Return the default key-value view for this table."""

        return self.key_value()

    def read_pipeline(self) -> "TableReadPipeline":
        """Return a Redis-style read pipeline for the table's key view.

        Commands are read-only and are executed together by one indexed
        multi-get.  Use :meth:`transaction` when writes need a durable group
        commit.
        """

        return self.kv.read_pipeline()

    def cache_stats(self) -> dict[str, int]:
        """Return process-local fixed-table cache statistics."""

        if self._fixed_store is None or not self._fixed_store.active:
            return {
                "row_cache_hits": 0,
                "row_cache_misses": 0,
                "row_cache_evictions": 0,
                "projection_cache_hits": 0,
                "projection_cache_misses": 0,
                "projection_cache_evictions": 0,
                "get_calls": 0,
                "get_many_calls": 0,
                "generation_retries": 0,
                "index_hits": 0,
                "index_rebuilds": 0,
                "index_persisted_hits": 0,
                "row_cache_size": 0,
                "projection_cache_size": 0,
                "key_cache_size": 0,
            }
        return self._fixed_store.cache_stats()

    def get_by_key(self, key: Any, key_column: Optional[str] = None) -> dict[str, Any]:
        """Read one row through the key-value index."""

        return self.key_value(key_column).require(key)

    def update_by_key(
        self,
        key: Any,
        values: Mapping[str, Any],
        key_column: Optional[str] = None,
    ) -> int:
        """Patch one indexed row and return one, or raise ``KeyError``."""

        return self.key_value(key_column).update(key, values)

    def delete_by_key(self, key: Any, key_column: Optional[str] = None) -> int:
        """Delete one indexed row and return one, or raise ``KeyError``."""

        return self.key_value(key_column).delete(key)

    def update_by_pk(self, key: Any, values: Mapping[str, Any]) -> int:
        """Update one row by primary key and return the affected row count."""

        if self.definition is None:
            raise KeyError(key)
        patch = self._normalise_row(values)
        key_values = self._normalise_primary_key(key)

        if self._fixed_store is not None and self._fixed_store.active:
            return self._fixed_store.update(self.definition.primary_keys, key_values, patch)

        def apply(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
            position = self._primary_key_position_in_snapshot(snapshot, key)
            updated = dict(rows[position])
            updated.update(copy.deepcopy(patch))
            rows[position] = updated
            return 1, {"op": "update_pk", "key": list(key_values), "values": patch, "columns": patch.keys()}

        return int(self._mutate_incremental(apply))

    def delete_by_pk(self, key: Any) -> int:
        """Delete one row by primary key and return the affected row count."""

        if self.definition is None:
            raise KeyError(key)
        key_values = self._normalise_primary_key(key)

        if self._fixed_store is not None and self._fixed_store.active:
            return self._fixed_store.delete(self.definition.primary_keys, key_values)

        def remove(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
            position = self._primary_key_position_in_snapshot(snapshot, key)
            del rows[position]
            return 1, {"op": "delete_pk", "key": list(key_values)}

        return int(self._mutate_incremental(remove))

    def _normalise_primary_key(self, key: Any) -> tuple[Any, ...]:
        if self.definition is None:
            raise KeyError(key)
        if len(self.definition.primary_keys) == 1:
            values = (key,)
        elif isinstance(key, (tuple, list)) and len(key) == len(self.definition.primary_keys):
            values = tuple(key)
        else:
            raise KeyError(key)
        try:
            json.dumps(list(values), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise KeyError(key) from exc
        return values

    # ---- relation-oriented API -------------------------------------------------
    def select(
        self,
        where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]] = None,
        *,
        columns: Optional[Sequence[str]] = None,
        order_by: Optional[Union[str, Sequence[Any]]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        predicate = _compile_where(where)
        fixed_equality_where: Optional[Mapping[str, Any]] = None
        if isinstance(where, Mapping) and where:
            fixed_equality_where = where
        elif self._fixed_store is not None and self._fixed_store.active:
            fixed_equality_where = _parse_equality_mapping(where)
        if (
            self._fixed_store is not None
            and self._fixed_store.active
            and self.definition is not None
            and fixed_equality_where is not None
            and len(fixed_equality_where) == len(self.definition.primary_keys)
            and {str(field) for field in fixed_equality_where} == set(self.definition.primary_keys)
        ):
            if limit == 0 or offset > 0:
                return []
            key = tuple(fixed_equality_where[column] for column in self.definition.primary_keys)
            selected: Optional[list[str]] = None
            fetch_fields: Optional[tuple[str, ...]] = None
            if columns is not None:
                column_items = [columns] if isinstance(columns, str) else columns
                selected = [_unquote_identifier(str(column)) for column in column_items]
                fetch_fields = tuple(dict.fromkeys(tuple(self.definition.primary_keys) + tuple(selected)))
            try:
                row = self._fixed_store.get(self.definition.primary_keys, key, fields=fetch_fields)
            except KeyError:
                return []
            if not predicate(row):
                return []
            if selected is not None:
                row = {column: row.get(column) for column in selected}
                return [row]
            # ``_FixedRecordStore.get`` already returns a detached copy.  A
            # second deepcopy here used to dominate hot primary-key SELECTs
            # for primitive fixed-width columns.
            return [row]
        if (
            self._fixed_store is not None
            and self._fixed_store.active
            and fixed_equality_where is not None
        ):
            # A pure equality mapping can be answered from fixed slots.  The
            # backend returns ``None`` when a requested field cannot be
            # indexed or projected, so the general snapshot path below keeps
            # the existing behaviour for unknown, unhashable and unusual
            # predicates.
            equal_columns = tuple(str(field) for field in fixed_equality_where)
            try:
                equal_key = tuple(fixed_equality_where[field] for field in fixed_equality_where)
                selected_columns: Optional[tuple[str, ...]] = None
                if columns is not None:
                    column_items = [columns] if isinstance(columns, str) else columns
                    selected_columns = tuple(_unquote_identifier(str(column)) for column in column_items)
                indexed_rows = self._fixed_store.select_equal(
                    equal_columns,
                    equal_key,
                    columns=selected_columns,
                    order_by=order_by,
                    limit=limit,
                    offset=offset,
                )
            except (KeyError, TypeError):
                indexed_rows = None
            if indexed_rows is not None:
                return indexed_rows
        snapshot = self._read_snapshot()
        positions = self._candidate_positions(snapshot, where)
        if positions is None:
            source = enumerate(snapshot.rows)
        else:
            source = ((position, snapshot.rows[position]) for position in positions)
        # A user-supplied callable may mutate its argument.  Mapping and SQL
        # predicates are read-only, so they can inspect the cached row directly
        # and avoid copying the full table before filtering.
        callable_predicate = callable(where)
        rows: list[dict[str, Any]] = []
        stop_after = None if order_by is not None or limit is None else offset + limit
        for _position, row in source:
            candidate = copy.deepcopy(row) if callable_predicate else row
            if predicate(candidate):
                rows.append(row)
                if stop_after is not None and len(rows) >= stop_after:
                    break
        _sort_rows(rows, order_by)
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        if columns is not None:
            column_items = [columns] if isinstance(columns, str) else columns
            selected = [_unquote_identifier(str(column)) for column in column_items]
            rows = [{column: row.get(column) for column in selected} for row in rows]
        return copy.deepcopy(rows)

    def find(self, where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]] = None, *, order_by: Optional[Union[str, Sequence[Any]]] = None) -> Optional[dict[str, Any]]:
        rows = self.select(where, order_by=order_by, limit=1)
        return rows[0] if rows else None

    def all(self) -> list[dict[str, Any]]:
        """Return a detached list containing the current rows."""

        return self.select()

    def count(self, where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]] = None) -> int:
        if where is None and self._fixed_store is not None and self._fixed_store.active:
            return self._fixed_store.row_count()
        predicate = _compile_where(where)
        snapshot = self._read_snapshot()
        positions = self._candidate_positions(snapshot, where)
        rows = snapshot.rows if positions is None else (snapshot.rows[position] for position in positions)
        if callable(where):
            return sum(1 for row in rows if predicate(copy.deepcopy(row)))
        return sum(1 for row in rows if predicate(row))

    def insert_one(self, row: Mapping[str, Any]) -> int:
        row = self._normalise_row(row)

        if self._fixed_store is not None and self._fixed_store.active:
            return self._fixed_store.insert_many([row])

        def add(rows: list[dict[str, Any]], columns: list[str], _snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
            index = len(rows)
            rows.append(row)
            return index, {"op": "append", "rows": [row], "columns": row.keys()}

        return int(self._mutate_incremental(add))

    def insert_many(self, rows: Iterable[Mapping[str, Any]]) -> int:
        values = [self._normalise_row(row) for row in rows]

        if not values:
            return 0

        if self._fixed_store is not None and self._fixed_store.active:
            return self._fixed_store.insert_many(values)

        if len(values) > _WAL_BATCH_MAX_ROWS:
            def add_snapshot(current: list[dict[str, Any]], columns: list[str]) -> int:
                current.extend(values)
                return len(values)

            new_columns = {key for row in values for key in row}
            return int(self._mutate(add_snapshot, copy_rows=False, new_columns=new_columns))

        def add(current: list[dict[str, Any]], columns: list[str], _snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
            current.extend(values)
            return len(values), {
                "op": "append",
                "rows": values,
                "columns": (key for row in values for key in row),
            }

        return int(self._mutate_incremental(add))

    def update(
        self,
        values: Mapping[str, Any],
        where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]] = None,
        *,
        limit: Optional[int] = None,
    ) -> int:
        if not isinstance(values, Mapping):
            raise TypeError("values must be a mapping")
        patch = self._normalise_row(values)
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        predicate = _compile_where(where)

        indexed_where: Optional[Mapping[str, Any]] = None
        if isinstance(where, Mapping) and where:
            indexed_where = where
        elif self._fixed_store is not None and self._fixed_store.active:
            indexed_where = _parse_equality_mapping(where)

        if indexed_where is not None:
            try:
                where_values = copy.deepcopy(dict(indexed_where))
                json.dumps(where_values, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"where contains a non-JSON value: {exc}") from exc

            if self._fixed_store is not None and self._fixed_store.active and self.definition is not None:
                key_columns = self.definition.primary_keys
                if len(where_values) == len(key_columns) and {str(field) for field in where_values} == set(key_columns):
                    if limit == 0:
                        return 0
                    key_values = tuple(where_values[column] for column in key_columns)
                    try:
                        return self._fixed_store.update(key_columns, key_values, patch)
                    except KeyError:
                        return 0

                equal_columns = tuple(str(field) for field in where_values)
                try:
                    equal_key = tuple(where_values[field] for field in where_values)
                    indexed_changed = self._fixed_store.update_equal(
                        equal_columns,
                        equal_key,
                        patch,
                        limit=limit,
                    )
                except (KeyError, TypeError):
                    indexed_changed = None
                if indexed_changed is not None:
                    return indexed_changed

            def apply_mapping(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
                changed = 0
                positions = self._candidate_positions(snapshot, where_values)
                selected = range(len(rows)) if positions is None else positions
                for position in selected:
                    row = rows[position]
                    if predicate(row) and (limit is None or changed < limit):
                        updated = dict(row)
                        updated.update(copy.deepcopy(patch))
                        rows[position] = updated
                        changed += 1
                return changed, {
                    "op": "update_where",
                    "where": where_values,
                    "values": patch,
                    "limit": limit,
                    "columns": patch.keys(),
                }

            return int(self._mutate_incremental(apply_mapping))

        def apply(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> int:
            changed = 0
            positions = self._candidate_positions(snapshot, where)
            selected = range(len(rows)) if positions is None else positions
            for position in selected:
                row = rows[position]
                candidate = copy.deepcopy(row) if callable(where) else row
                if predicate(candidate) and (limit is None or changed < limit):
                    rows[position] = copy.deepcopy(row)
                    rows[position].update(copy.deepcopy(patch))
                    changed += 1
            return changed

        return int(self._mutate_with_snapshot(apply, copy_rows=False, new_columns=patch.keys()))

    def update_many_where(
        self,
        updates: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]],
    ) -> int:
        """Apply multiple equality updates in one safe table transaction.

        Each item is ``(where_mapping, values_mapping)``.  Schema-defined
        fixed tables resolve all predicates and publish one WAL frame, so a
        batch pays the two durability syncs once instead of once per row.
        Unsupported predicates or snapshot-backed tables use the
        existing per-update implementation.
        """

        prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in updates:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise TypeError("updates must contain (where, values) pairs")
            where, values = item
            if not isinstance(where, Mapping) or not where:
                raise TypeError("batch where clauses must be non-empty mappings")
            if not isinstance(values, Mapping):
                raise TypeError("batch update values must be mappings")
            where_values = copy.deepcopy(dict(where))
            try:
                json.dumps(where_values, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"where contains a non-JSON value: {exc}") from exc
            prepared.append((where_values, self._normalise_row(values)))
        if not prepared:
            return 0
        if self._fixed_store is not None and self._fixed_store.active and self.definition is not None:
            operations = [
                (
                    tuple(str(column) for column in where),
                    tuple(where[column] for column in where),
                    values,
                )
                for where, values in prepared
            ]
            changed = self._fixed_store.update_equal_many(operations)
            if changed is not None:
                return changed
        return sum(self.update(values, where) for where, values in prepared)

    def delete(
        self,
        where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]] = None,
        *,
        limit: Optional[int] = None,
    ) -> int:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        predicate = _compile_where(where)

        indexed_where: Optional[Mapping[str, Any]] = None
        if isinstance(where, Mapping) and where:
            indexed_where = where
        elif self._fixed_store is not None and self._fixed_store.active:
            indexed_where = _parse_equality_mapping(where)

        if indexed_where is not None:
            try:
                where_values = copy.deepcopy(dict(indexed_where))
                json.dumps(where_values, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"where contains a non-JSON value: {exc}") from exc

            if self._fixed_store is not None and self._fixed_store.active and self.definition is not None:
                key_columns = self.definition.primary_keys
                if len(where_values) == len(key_columns) and {str(field) for field in where_values} == set(key_columns):
                    if limit == 0:
                        return 0
                    key_values = tuple(where_values[column] for column in key_columns)
                    try:
                        return self._fixed_store.delete(key_columns, key_values)
                    except KeyError:
                        return 0

                equal_columns = tuple(str(field) for field in where_values)
                try:
                    equal_key = tuple(where_values[field] for field in where_values)
                    indexed_removed = self._fixed_store.delete_equal(
                        equal_columns,
                        equal_key,
                        limit=limit,
                    )
                except (KeyError, TypeError):
                    indexed_removed = None
                if indexed_removed is not None:
                    return indexed_removed

            def remove_mapping(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
                removed = 0
                kept: list[dict[str, Any]] = []
                positions = self._candidate_positions(snapshot, where_values)
                selected = None if positions is None else set(positions)
                for position, row in enumerate(rows):
                    if (selected is None or position in selected) and predicate(row) and (limit is None or removed < limit):
                        removed += 1
                    else:
                        kept.append(row)
                rows[:] = kept
                return removed, {"op": "delete_where", "where": where_values, "limit": limit}

            return int(self._mutate_incremental(remove_mapping))

        def remove(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> int:
            removed = 0
            kept: list[dict[str, Any]] = []
            positions = self._candidate_positions(snapshot, where)
            selected = None if positions is None else set(positions)
            for position, row in enumerate(rows):
                candidate = copy.deepcopy(row) if callable(where) else row
                if (selected is None or position in selected) and predicate(candidate) and (limit is None or removed < limit):
                    removed += 1
                else:
                    kept.append(row)
            rows[:] = kept
            return removed

        return int(self._mutate_with_snapshot(remove, copy_rows=False))

    def delete_many_where(self, wheres: Iterable[Mapping[str, Any]]) -> int:
        """Delete multiple equality conditions in one safe table transaction."""

        prepared: list[dict[str, Any]] = []
        for where in wheres:
            if not isinstance(where, Mapping) or not where:
                raise TypeError("batch where clauses must be non-empty mappings")
            where_values = copy.deepcopy(dict(where))
            try:
                json.dumps(where_values, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"where contains a non-JSON value: {exc}") from exc
            prepared.append(where_values)
        if not prepared:
            return 0
        if self._fixed_store is not None and self._fixed_store.active and self.definition is not None:
            operations = [
                (
                    tuple(str(column) for column in where),
                    tuple(where[column] for column in where),
                )
                for where in prepared
            ]
            removed = self._fixed_store.delete_equal_many(operations)
            if removed is not None:
                return removed
        return sum(self.delete(where) for where in prepared)

    def sort(self, *, key: Optional[str] = None, reverse: bool = False) -> None:
        """Sort the physical row order, like ``list.sort``."""

        order = None if key is None else [(key, "DESC" if reverse else "ASC")]

        def reorder(rows: list[dict[str, Any]], columns: list[str]) -> None:
            _sort_rows(rows, order)

        self._mutate(reorder, copy_rows=False)

    # ---- list-like API ---------------------------------------------------------
    def __len__(self) -> int:
        if self._fixed_store is not None and self._fixed_store.active:
            return self._fixed_store.row_count()
        return len(self._read_snapshot().rows)

    def _close(self) -> None:
        if self._fixed_store is not None:
            self._fixed_store.close()
        with self._cache_guard:
            self._cache = None
            self._kv_view = None

    def __del__(self) -> None:  # pragma: no cover - best-effort interpreter cleanup
        # Direct ``Table(...)`` users do not have a database container whose
        # ``close()`` method can release the long-lived mmap/WAL handles.  A
        # key-value view forms a small reference cycle with its table, so rely
        # on the PEP 442 finalizer path as a final safety net at interpreter
        # shutdown rather than leaving descriptors to ResourceWarning.
        try:
            self._close()
        except Exception:
            pass

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._read_rows())

    def __getitem__(self, index: Union[int, slice]) -> Union[dict[str, Any], list[dict[str, Any]]]:
        rows = self._read_rows()
        if isinstance(index, slice):
            return rows[index]
        return rows[index]

    def __setitem__(self, index: Union[int, slice], value: Union[Mapping[str, Any], Iterable[Mapping[str, Any]]]) -> None:
        if isinstance(index, slice):
            if isinstance(value, Mapping):
                raise TypeError("slice assignment requires an iterable of rows")
            replacement = [self._normalise_row(row) for row in value]
        else:
            replacement = self._normalise_row(value)  # type: ignore[arg-type]

        def assign(rows: list[dict[str, Any]], columns: list[str]) -> None:
            if isinstance(index, slice):
                rows[index] = replacement  # type: ignore[index]
            else:
                rows[index] = replacement  # type: ignore[list-item]

        self._mutate(assign, copy_rows=False, new_columns=(key for row in replacement for key in row))

    def __delitem__(self, index: Union[int, slice]) -> None:
        def remove(rows: list[dict[str, Any]], columns: list[str]) -> None:
            del rows[index]

        self._mutate(remove, copy_rows=False)

    def insert(self, index: int, value: Mapping[str, Any]) -> None:
        row = self._normalise_row(value)

        def add(rows: list[dict[str, Any]], columns: list[str]) -> None:
            rows.insert(index, row)

        self._mutate(add, copy_rows=False, new_columns=row.keys())

    def append(self, value: Mapping[str, Any]) -> None:
        self.insert_one(value)

    def extend(self, values: Iterable[Mapping[str, Any]]) -> None:
        self.insert_many(values)

    def pop(self, index: int = -1) -> dict[str, Any]:
        def remove(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, Any]:
            return rows.pop(index)

        return copy.deepcopy(self._mutate(remove, copy_rows=False))

    def clear(self) -> None:
        self.delete()

    # ---- SQL-like API ----------------------------------------------------------
    def execute(self, statement: str) -> QueryResult:
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError("statement must be a non-empty string")
        statement = statement.strip().rstrip(";").strip()
        keyword = statement.split(None, 1)[0].upper()
        if keyword == "SELECT":
            return self._execute_select(statement)
        if keyword == "INSERT":
            return self._execute_insert(statement)
        if keyword == "UPDATE":
            return self._execute_update(statement)
        if keyword == "DELETE":
            return self._execute_delete(statement)
        raise ValueError(f"unsupported SQL statement: {keyword}")

    def _assert_table(self, token: str) -> None:
        if _parse_table_name(token) != self.name:
            raise ValueError(f"statement targets table {_parse_table_name(token)!r}, not {self.name!r}")

    @staticmethod
    def _clauses(text: str, names: Sequence[str]) -> dict[str, str]:
        positions: list[tuple[int, str]] = []
        upper = text.upper()
        quote: Optional[str] = None
        depth = 0
        index = 0
        while index < len(text):
            char = text[index]
            if quote:
                if char == quote and (index == 0 or text[index - 1] != "\\"):
                    quote = None
                index += 1
                continue
            if char in ("'", '"'):
                quote = char
                index += 1
                continue
            if char == "(":
                depth += 1
                index += 1
                continue
            if char == ")":
                depth -= 1
                index += 1
                continue
            if depth == 0:
                for name in names:
                    if upper.startswith(name, index):
                        before = text[index - 1] if index else " "
                        after_index = index + len(name)
                        after = text[after_index] if after_index < len(text) else " "
                        if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                            positions.append((index, name))
                            index = after_index
                            break
                else:
                    index += 1
                continue
            index += 1
        positions.sort()
        result: dict[str, str] = {}
        for pos, (start, name) in enumerate(positions):
            end = positions[pos + 1][0] if pos + 1 < len(positions) else len(text)
            result[name] = text[start + len(name) : end].strip()
        return result

    def _execute_select(self, statement: str) -> QueryResult:
        match = re.match(rf"^SELECT\s+(?P<columns>.+?)\s+FROM\s+(?P<table>{_IDENTIFIER})(?P<tail>.*)$", statement, re.IGNORECASE | re.DOTALL)
        if not match:
            raise ValueError("invalid SELECT syntax")
        self._assert_table(match.group("table"))
        selected = match.group("columns").strip()
        clauses = self._clauses(match.group("tail"), ("WHERE", "ORDER BY", "LIMIT", "OFFSET"))
        where = clauses.get("WHERE")
        order_by = clauses.get("ORDER BY")
        limit = int(clauses["LIMIT"]) if "LIMIT" in clauses else None
        offset = int(clauses["OFFSET"]) if "OFFSET" in clauses else 0
        if selected == "*":
            rows = self.select(where, order_by=order_by, limit=limit, offset=offset)
        else:
            projection: list[str] = []
            output_names: list[str] = []
            for item in _split_top_level(selected):
                alias_match = re.match(rf"^(?P<field>{_IDENTIFIER}(?:\.{_IDENTIFIER})?)\s+AS\s+(?P<alias>{_IDENTIFIER})$", item, re.IGNORECASE)
                if alias_match:
                    field = _unquote_identifier(alias_match.group("field"))
                    output_names.append(_unquote_identifier(alias_match.group("alias")))
                else:
                    field = _unquote_identifier(item)
                    output_names.append(field.rsplit(".", 1)[-1])
                projection.append(field.rsplit(".", 1)[-1])
            rows = self.select(where, columns=projection, order_by=order_by, limit=limit, offset=offset)
            rows = [{output: row.get(source) for source, output in zip(projection, output_names)} for row in rows]
        return QueryResult(rows)

    def _execute_insert(self, statement: str) -> QueryResult:
        match = re.match(rf"^INSERT\s+INTO\s+(?P<table>{_IDENTIFIER})\s*(?:\((?P<columns>[^)]*)\))?\s+VALUES\s+(?P<values>.+)$", statement, re.IGNORECASE | re.DOTALL)
        if not match:
            raise ValueError("invalid INSERT syntax")
        self._assert_table(match.group("table"))
        columns = [_validate_identifier(item.strip(), label="column name") for item in _split_top_level(match.group("columns"))] if match.group("columns") else None
        raw_values = match.group("values").strip()
        groups: list[str] = []
        index = 0
        while index < len(raw_values):
            while index < len(raw_values) and raw_values[index].isspace():
                index += 1
            if index >= len(raw_values):
                break
            if raw_values[index] != "(":
                raise ValueError("INSERT VALUES must contain parenthesised rows")
            depth = 0
            quote: Optional[str] = None
            start = index
            while index < len(raw_values):
                char = raw_values[index]
                if quote:
                    if char == quote and raw_values[index - 1] != "\\":
                        quote = None
                elif char in ("'", '"'):
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            groups.append(raw_values[start:index])
            while index < len(raw_values) and raw_values[index].isspace():
                index += 1
            if index < len(raw_values):
                if raw_values[index] != ",":
                    raise ValueError("invalid separator between INSERT rows")
                index += 1
        parsed: list[dict[str, Any]] = []
        existing_columns = self._read_snapshot_columns()
        for group in groups:
            values = [_parse_scalar(item) for item in _split_top_level(group[1:-1])]
            target_columns = columns or existing_columns
            if not target_columns:
                raise ValueError("INSERT into an empty table requires a column list")
            if len(values) != len(target_columns):
                raise ValueError("INSERT column/value count mismatch")
            parsed.append(dict(zip(target_columns, values)))
        def add(rows: list[dict[str, Any]], columns: list[str]) -> tuple[int, int]:
            first = len(rows)
            rows.extend(parsed)
            return first, len(parsed)

        first, count = self._mutate(add, copy_rows=False, new_columns=(key for row in parsed for key in row))
        return QueryResult(rowcount=count, lastrowid=(first if count else None))

    def _read_snapshot_columns(self) -> list[str]:
        if self._fixed_store is not None and self._fixed_store.active and self.definition is not None:
            return list(self.definition.column_names)
        return list(self._read_snapshot().columns)

    def _execute_update(self, statement: str) -> QueryResult:
        match = re.match(rf"^UPDATE\s+(?P<table>{_IDENTIFIER})\s+SET\s+(?P<tail>.+)$", statement, re.IGNORECASE | re.DOTALL)
        if not match:
            raise ValueError("invalid UPDATE syntax")
        self._assert_table(match.group("table"))
        clauses = self._clauses(match.group("tail"), ("WHERE", "LIMIT"))
        assignments_text = clauses.get("WHERE", clauses.get("LIMIT", match.group("tail")))
        if "WHERE" in clauses:
            assignments_text = match.group("tail")[: match.group("tail").upper().find("WHERE")].strip()
        elif "LIMIT" in clauses:
            assignments_text = match.group("tail")[: match.group("tail").upper().find("LIMIT")].strip()
        values: dict[str, Any] = {}
        for item in _split_top_level(assignments_text):
            if "=" not in item:
                raise ValueError(f"invalid UPDATE assignment: {item!r}")
            field, raw = item.split("=", 1)
            values[_validate_identifier(field.strip(), label="column name")] = _parse_scalar(raw)
        limit = int(clauses["LIMIT"]) if "LIMIT" in clauses else None
        return QueryResult(rowcount=self.update(values, clauses.get("WHERE"), limit=limit))

    def _execute_delete(self, statement: str) -> QueryResult:
        match = re.match(rf"^DELETE\s+FROM\s+(?P<table>{_IDENTIFIER})(?P<tail>.*)$", statement, re.IGNORECASE | re.DOTALL)
        if not match:
            raise ValueError("invalid DELETE syntax")
        self._assert_table(match.group("table"))
        clauses = self._clauses(match.group("tail"), ("WHERE", "LIMIT"))
        limit = int(clauses["LIMIT"]) if "LIMIT" in clauses else None
        return QueryResult(rowcount=self.delete(clauses.get("WHERE"), limit=limit))


class TableWriteBatch:
    """Explicit group-commit boundary for schema-defined fixed tables.

    The regular :class:`Table` write methods return after their own WAL
    ``fsync``; the table mapping is checkpointed later without weakening that
    recovery guarantee.  This context object lets a caller commit many
    updates, deletes, or inserts in one WAL frame.  Keeping one operation family
    per batch means the fixed backend can publish the complete set in one WAL
    frame and reject ambiguous mixed-operation atomicity.
    """

    def __init__(self, table: Table) -> None:
        self.table = table
        self._operations: list[tuple[str, Any]] = []
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("write batch is already closed")
        store = self.table._fixed_store
        if store is None or not store.active or self.table.definition is None:
            raise StorageError("write batches require a schema-defined fixed table")

    def _queue(self, kind: str, value: Any) -> "TableWriteBatch":
        self._check_open()
        if self._operations and self._operations[0][0] != kind:
            raise ValueError("a write batch must contain only updates, deletes, or inserts")
        self._operations.append((kind, value))
        return self

    def update(
        self,
        values: Mapping[str, Any],
        where: Mapping[str, Any] | str,
        *,
        limit: Optional[int] = None,
    ) -> "TableWriteBatch":
        """Queue one equality update without committing it yet."""

        if limit is not None:
            raise ValueError("write-batch updates do not accept LIMIT; queue only equality predicates")
        if not isinstance(where, Mapping):
            where_values = _parse_equality_mapping(where)
            if where_values is None:
                raise ValueError("write-batch updates require an equality mapping")
        else:
            where_values = dict(where)
        if not where_values:
            raise ValueError("write-batch updates require a non-empty equality mapping")
        patch = self.table._normalise_row(values)
        try:
            json.dumps(where_values, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"where contains a non-JSON value: {exc}") from exc
        return self._queue(
            "update",
            (
                tuple(str(column) for column in where_values),
                tuple(where_values[column] for column in where_values),
                patch,
            ),
        )

    def update_by_pk(self, key: Any, values: Mapping[str, Any]) -> "TableWriteBatch":
        """Queue one primary-key patch."""

        self._check_open()
        assert self.table.definition is not None
        key_values = self.table._normalise_primary_key(key)
        return self.update(
            values,
            {column: value for column, value in zip(self.table.definition.primary_keys, key_values)},
        )

    def update_by_key(
        self,
        key: Any,
        values: Mapping[str, Any],
        key_column: Optional[str] = None,
    ) -> "TableWriteBatch":
        """Queue one key-value-view patch."""

        self._check_open()
        view = self.table.key_value(key_column)
        if not view._declared_primary:
            raise ValueError("write-batch update_by_key requires the declared primary-key view")
        key_values = view._normalise_key(key)
        patch = self.table._normalise_row(values)
        view._reject_key_patch(patch, key_values)
        return self.update(
            patch,
            {column: value for column, value in zip(view.key_columns, key_values)},
        )

    def delete(
        self,
        where: Mapping[str, Any] | str,
        *,
        limit: Optional[int] = None,
    ) -> "TableWriteBatch":
        """Queue one equality delete without committing it yet."""

        if limit is not None:
            raise ValueError("write-batch deletes do not accept LIMIT; queue equality predicates")
        if not isinstance(where, Mapping):
            where_values = _parse_equality_mapping(where)
            if where_values is None:
                raise ValueError("write-batch deletes require an equality mapping")
        else:
            where_values = dict(where)
        if not where_values:
            raise ValueError("write-batch deletes require a non-empty equality mapping")
        try:
            json.dumps(where_values, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"where contains a non-JSON value: {exc}") from exc
        return self._queue(
            "delete",
            (
                tuple(str(column) for column in where_values),
                tuple(where_values[column] for column in where_values),
            ),
        )

    def delete_by_pk(self, key: Any) -> "TableWriteBatch":
        """Queue one primary-key delete."""

        self._check_open()
        assert self.table.definition is not None
        key_values = self.table._normalise_primary_key(key)
        return self.delete({column: value for column, value in zip(self.table.definition.primary_keys, key_values)})

    def delete_by_key(self, key: Any, key_column: Optional[str] = None) -> "TableWriteBatch":
        """Queue one key-value-view delete."""

        self._check_open()
        view = self.table.key_value(key_column)
        if not view._declared_primary:
            raise ValueError("write-batch delete_by_key requires the declared primary-key view")
        key_values = view._normalise_key(key)
        return self.delete({column: value for column, value in zip(view.key_columns, key_values)})

    def insert(self, row: Mapping[str, Any]) -> "TableWriteBatch":
        """Queue one insert; use :meth:`insert_many` for a prepared sequence."""

        return self.insert_many([row])

    def insert_many(self, rows: Iterable[Mapping[str, Any]]) -> "TableWriteBatch":
        """Queue inserts for one durable batch commit."""

        values = [self.table._normalise_row(row) for row in rows]
        if not values:
            return self
        return self._queue("insert", values)

    def commit(self) -> int:
        """Publish the queued operation family with one durable commit."""

        self._check_open()
        try:
            if not self._operations:
                return 0
            kind = self._operations[0][0]
            store = self.table._fixed_store
            assert store is not None
            if kind == "update":
                changed = store.update_equal_many([item for _kind, item in self._operations])
            elif kind == "delete":
                changed = store.delete_equal_many([item for _kind, item in self._operations])
            else:
                changed = store.insert_many([row for _kind, rows in self._operations for row in rows])
            if changed is None:
                raise ValueError("write-batch predicate cannot use a fixed equality index")
            return int(changed)
        finally:
            self._closed = True

    def rollback(self) -> None:
        """Discard queued operations without touching the table."""

        self._check_open()
        self._operations.clear()
        self._closed = True

    def __enter__(self) -> "TableWriteBatch":
        self._check_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._closed:
            return
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


class TableKeyValue(MutableMapping[Any, dict[str, Any]]):
    """Indexed dictionary-like access to rows in a :class:`Table`.

    A view is cheap to create and does not keep a mutable in-memory copy.  Each
    read obtains the table's immutable snapshot and each write is committed by
    the table's existing cross-process lock and WAL transaction.  The default
    key is the JSON definition's primary key; an explicitly supplied column
    must identify exactly one row for every lookup.

    ``update`` applies a patch, while ``set`` replaces the complete row (the
    requested key columns are filled in when omitted).  Key columns are
    immutable through this view so that a caller cannot silently invalidate
    the index or change the meaning of an in-flight update.
    """

    def __init__(self, table: Table, *, key_column: Optional[str] = None) -> None:
        self.table = table
        self._declared_primary = key_column is None and table.definition is not None and bool(table.definition.primary_keys)
        if key_column is None:
            if table.definition is not None and table.definition.primary_keys:
                self.key_columns = tuple(table.definition.primary_keys)
            else:
                self.key_columns = ("id",)
        else:
            column = _validate_identifier(str(key_column), label="key column")
            if table.definition is not None and column not in table.definition.column_names:
                raise KeyError(column)
            self.key_columns = (column,)
        if not self.key_columns:
            raise ValueError("at least one key column is required")

    @property
    def key_column(self) -> Optional[str]:
        """Return the selected column for a single-column view."""

        return self.key_columns[0] if len(self.key_columns) == 1 else None

    def _normalise_key(self, key: Any, *, for_write: bool = True) -> tuple[Any, ...]:
        if len(self.key_columns) == 1:
            values = (key,)
        elif isinstance(key, (tuple, list)) and len(key) == len(self.key_columns):
            values = tuple(key)
        else:
            raise KeyError(key)
        try:
            if for_write:
                json.dumps(list(values), ensure_ascii=False, allow_nan=False)
            hash(values)
        except (TypeError, ValueError) as exc:
            raise KeyError(key) from exc
        return values

    def _index(self, snapshot: _Snapshot) -> dict[tuple[Any, ...], tuple[int, ...]]:
        if self._declared_primary:
            # The definition validator guarantees uniqueness.  The snapshot
            # is immutable from the caller's perspective, so avoid copying
            # this potentially large dictionary on every point lookup.
            return snapshot.primary_index
        if len(self.key_columns) == 1:
            column = self.key_columns[0]
            raw = snapshot.indexes.get(column)
            if raw is None:
                raw_column = _build_column_index(snapshot.rows, column)
                if raw_column is None:
                    raise ValueError(f"key column {column!r} is missing or contains an unhashable value")
                with self.table._cache_guard:
                    snapshot.indexes[column] = raw_column
                raw = raw_column
            return { (value,): positions for value, positions in raw.items() }
        raw_composite = _build_key_positions(snapshot.rows, self.key_columns)
        if raw_composite is None:
            raise ValueError(f"key columns {self.key_columns!r} are missing or contain an unhashable value")
        return raw_composite

    def _positions(self, snapshot: _Snapshot, values: tuple[Any, ...]) -> tuple[int, ...]:
        """Look up positions without rebuilding a tuple-key adapter."""

        if self._declared_primary:
            return snapshot.primary_index.get(values, ())
        if len(self.key_columns) == 1:
            column = self.key_columns[0]
            raw = snapshot.indexes.get(column)
            if raw is None:
                raw = _build_column_index(snapshot.rows, column)
                if raw is None:
                    raise ValueError(f"key column {column!r} is missing or contains an unhashable value")
                with self.table._cache_guard:
                    snapshot.indexes[column] = raw
            return raw.get(values[0], ())
        return self._index(snapshot).get(values, ())

    def _position(self, snapshot: _Snapshot, key: Any) -> int:
        values = self._normalise_key(key, for_write=False)
        positions = self._positions(snapshot, values)
        if not positions:
            raise KeyError(key)
        if len(positions) != 1:
            raise ValueError(f"key {key!r} is not unique in table {self.table.name!r}")
        return positions[0]

    def _key_for_row(self, row: Mapping[str, Any]) -> Any:
        try:
            values = tuple(row[column] for column in self.key_columns)
            hash(values)
        except (KeyError, TypeError) as exc:
            raise ValueError(f"key columns {self.key_columns!r} are missing or unhashable") from exc
        return values[0] if len(values) == 1 else values

    def _reject_key_patch(self, values: Mapping[str, Any], key: tuple[Any, ...]) -> None:
        for column, expected in zip(self.key_columns, key):
            if column in values and values[column] != expected:
                raise ValueError(f"key column {column!r} cannot be changed through a key-value view")

    def require(self, key: Any, *, fields: Optional[Sequence[str]] = None) -> dict[str, Any]:
        """Return one detached row, optionally restricted to ``fields``."""

        selected_fields: Optional[Sequence[str]] = (
            [fields] if isinstance(fields, str) else fields
        )
        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.get(
                self.key_columns,
                self._normalise_key(key, for_write=False),
                fields=selected_fields,
            )
        snapshot = self.table._read_snapshot()
        row = snapshot.rows[self._position(snapshot, key)]
        if selected_fields is None:
            return copy.deepcopy(row)
        selected = tuple(_unquote_identifier(str(field)) for field in selected_fields)
        return {field: copy.deepcopy(row.get(field)) for field in selected}

    def get(self, key: Any, default: Any = None, *, fields: Optional[Sequence[str]] = None) -> Any:
        """Return one detached row or ``default`` when the key is absent."""

        try:
            return self.require(key, fields=fields)
        except KeyError:
            return default

    def contains(self, key: Any) -> bool:
        try:
            self.require(key)
        except KeyError:
            return False
        return True

    def update(self, key: Any, values: Optional[Mapping[str, Any]] = None, *, upsert: bool = False) -> int:
        """Patch one row, or pass a mapping/iterable to :meth:`update_many`."""

        if values is None:
            if isinstance(key, Mapping):
                return self.update_many(key.items())
            raise TypeError("values must be a mapping")
        if not isinstance(values, Mapping):
            raise TypeError("values must be a mapping")
        key_values = self._normalise_key(key)
        patch = self.table._normalise_row(values)
        self._reject_key_patch(patch, key_values)

        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.update(self.key_columns, key_values, patch, upsert=upsert)

        if upsert:
            row = self._row_with_key(key, patch)

            def apply_upsert(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
                try:
                    position = self._position(snapshot, key)
                except KeyError:
                    rows.append(row)
                    return 1, {"op": "append", "rows": [row], "columns": row.keys()}
                updated = dict(rows[position])
                updated.update(copy.deepcopy(patch))
                rows[position] = updated
                return 1, {
                    "op": "update_key",
                    "key_columns": list(self.key_columns),
                    "key": list(key_values),
                    "values": patch,
                    "columns": patch.keys(),
                }

            return int(self.table._mutate_incremental(apply_upsert))

        def apply(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
            position = self._position(snapshot, key)
            updated = dict(rows[position])
            updated.update(copy.deepcopy(patch))
            rows[position] = updated
            return 1, {
                "op": "update_key",
                "key_columns": list(self.key_columns),
                "key": list(key_values),
                "values": patch,
                "columns": patch.keys(),
            }

        return int(self.table._mutate_incremental(apply))

    def _row_with_key(self, key: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        key_values = self._normalise_key(key)
        row = self.table._normalise_row(values)
        self._reject_key_patch(row, key_values)
        for column, value in zip(self.key_columns, key_values):
            row.setdefault(column, value)
        return row

    def set(self, key: Any, row: Mapping[str, Any], *, upsert: bool = False) -> int:
        """Replace a row, optionally inserting it when the key is absent."""

        key_values = self._normalise_key(key)
        replacement = self._row_with_key(key, row)

        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.set(self.key_columns, key_values, replacement, upsert=upsert)

        def apply(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
            try:
                position = self._position(snapshot, key)
            except KeyError:
                if not upsert:
                    raise
                rows.append(replacement)
                return 1, {"op": "append", "rows": [replacement], "columns": replacement.keys()}
            rows[position] = replacement
            return 1, {
                "op": "replace_key",
                "key_columns": list(self.key_columns),
                "key": list(key_values),
                "values": replacement,
                "columns": replacement.keys(),
            }

        return int(self.table._mutate_incremental(apply))

    def delete(self, key: Any) -> int:
        """Delete one row by key and return one, or raise ``KeyError``."""

        key_values = self._normalise_key(key)

        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.delete(self.key_columns, key_values)

        def remove(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
            position = self._position(snapshot, key)
            del rows[position]
            return 1, {
                "op": "delete_key",
                "key_columns": list(self.key_columns),
                "key": list(key_values),
            }

        return int(self.table._mutate_incremental(remove))

    def get_many(self, keys: Iterable[Any]) -> dict[Any, dict[str, Any]]:
        """Read many keys from one snapshot while preserving requested order."""

        if self.table._fixed_store is not None and self.table._fixed_store.active:
            prepared = [self._normalise_key(key, for_write=False) for key in keys]
            return self.table._fixed_store.get_many(self.key_columns, prepared)
        snapshot = self.table._read_snapshot()
        result: dict[Any, dict[str, Any]] = {}
        for key in keys:
            values = self._normalise_key(key, for_write=False)
            positions = self._positions(snapshot, values)
            if not positions:
                continue
            if len(positions) != 1:
                raise ValueError(f"key {key!r} is not unique in table {self.table.name!r}")
            public_key = values[0] if len(values) == 1 else values
            result[public_key] = copy.deepcopy(snapshot.rows[positions[0]])
        return result

    def read_pipeline(self) -> "TableReadPipeline":
        """Create a read-only pipeline for indexed key commands."""

        return TableReadPipeline(self)

    def cache_stats(self) -> dict[str, int]:
        """Return the owning table's process-local cache statistics."""

        return self.table.cache_stats()

    @staticmethod
    def _normalise_updates(updates: Any) -> list[tuple[Any, Mapping[str, Any]]]:
        if isinstance(updates, Mapping):
            source = list(updates.items())
        else:
            source = list(updates)
        result: list[tuple[Any, Mapping[str, Any]]] = []
        for item in source:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise TypeError("updates must contain (key, values) pairs")
            key, values = item
            if not isinstance(values, Mapping):
                raise TypeError("update values must be mappings")
            result.append((key, values))
        return result

    def update_many(self, updates: Any) -> int:
        """Patch many distinct keys in one lock acquisition and WAL frame."""

        source = self._normalise_updates(updates)
        if not source:
            return 0
        prepared: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        seen: set[tuple[Any, ...]] = set()
        for key, values in source:
            key_values = self._normalise_key(key)
            if key_values in seen:
                raise ValueError(f"duplicate key in update_many: {key!r}")
            seen.add(key_values)
            patch = self.table._normalise_row(values)
            self._reject_key_patch(patch, key_values)
            prepared.append((key_values, patch))

        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.update_many(self.key_columns, prepared)

        def apply(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
            entries: list[dict[str, Any]] = []
            new_columns: list[str] = []
            for key_values, patch in prepared:
                position = self._position(snapshot, key_values if len(key_values) > 1 else key_values[0])
                updated = dict(rows[position])
                updated.update(copy.deepcopy(patch))
                rows[position] = updated
                entries.append({"key": list(key_values), "values": patch})
                new_columns.extend(patch.keys())
            return len(entries), {
                "op": "update_keys",
                "key_columns": list(self.key_columns),
                "entries": entries,
                "columns": new_columns,
            }

        return int(self.table._mutate_incremental(apply))

    def delete_many(self, keys: Iterable[Any]) -> int:
        """Delete many distinct keys in one lock acquisition and WAL frame."""

        prepared = [self._normalise_key(key) for key in keys]
        if not prepared:
            return 0
        if len(set(prepared)) != len(prepared):
            raise ValueError("duplicate key in delete_many")

        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.delete_many(self.key_columns, prepared)

        def remove(rows: list[dict[str, Any]], columns: list[str], snapshot: _Snapshot) -> tuple[int, dict[str, Any]]:
            positions = [self._position(snapshot, values if len(values) > 1 else values[0]) for values in prepared]
            for position in sorted(positions, reverse=True):
                del rows[position]
            return len(positions), {
                "op": "delete_keys",
                "key_columns": list(self.key_columns),
                "keys": [list(values) for values in prepared],
            }

        return int(self.table._mutate_incremental(remove))

    def keys(self) -> list[Any]:
        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.keys(self.key_columns)
        snapshot = self.table._read_snapshot()
        self._index(snapshot)  # validate uniqueness/indexability before exposing keys
        return [self._key_for_row(row) for row in snapshot.rows]

    def values(self) -> list[dict[str, Any]]:
        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.values()
        return [copy.deepcopy(row) for row in self.table._read_snapshot().rows]

    def items(self) -> list[tuple[Any, dict[str, Any]]]:
        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.items(self.key_columns)
        snapshot = self.table._read_snapshot()
        self._index(snapshot)
        return [(self._key_for_row(row), copy.deepcopy(row)) for row in snapshot.rows]

    def __getitem__(self, key: Any) -> dict[str, Any]:
        return self.require(key)

    def __setitem__(self, key: Any, row: Mapping[str, Any]) -> None:
        self.set(key, row, upsert=True)

    def __delitem__(self, key: Any) -> None:
        self.delete(key)

    def __contains__(self, key: object) -> bool:  # type: ignore[override]
        return self.contains(key)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.keys())

    def __len__(self) -> int:
        if self._declared_primary and self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.row_count()
        return len(self.keys())

    def __repr__(self) -> str:
        return f"TableKeyValue(table={self.table.name!r}, key_columns={self.key_columns!r})"


class TableReadPipeline:
    """Redis-style read-only pipeline backed by one table generation.

    ``get`` and ``contains`` commands are queued without touching the file.
    ``execute`` normalizes and coalesces all point reads into a single
    ``get_many`` call, then restores command order.  The fixed backend checks
    the generation before and after the batch; if another process publishes a
    generation while the pipeline is running, the whole read is retried by
    the underlying implementation rather than returning a mixed view.
    """

    def __init__(self, view: TableKeyValue) -> None:
        self.view = view
        self._commands: list[tuple[str, Any]] = []

    def get(self, key: Any) -> "TableReadPipeline":
        self._commands.append(("get", key))
        return self

    def get_many(self, keys: Iterable[Any]) -> "TableReadPipeline":
        self._commands.append(("get_many", tuple(keys)))
        return self

    def contains(self, key: Any) -> "TableReadPipeline":
        self._commands.append(("contains", key))
        return self

    def __len__(self) -> int:
        return len(self._commands)

    def clear(self) -> "TableReadPipeline":
        self._commands.clear()
        return self

    def execute(self) -> list[Any]:
        """Execute queued commands and return one result per command."""

        commands = self._commands
        self._commands = []
        if not commands:
            return []

        # Flatten into one normalized key list.  ``get_many`` already uses the
        # persisted key index and batched LRU lock path for fixed tables.
        normalized: list[tuple[Any, tuple[Any, ...]]] = []
        for kind, value in commands:
            if kind == "get_many":
                for key in value:
                    normalized.append((key, self.view._normalise_key(key, for_write=False)))
            else:
                normalized.append((value, self.view._normalise_key(value, for_write=False)))
        normalized_keys = [normalized_key for _public, normalized_key in normalized]
        fixed_store = self.view.table._fixed_store
        rows: dict[Any, dict[str, Any]]
        if fixed_store is not None and fixed_store.active:
            # Keep the generation that produced this batch.  A commit that
            # overlaps command reconstruction causes the whole pipeline to be
            # retried, preserving the same optimistic snapshot rule as the
            # direct fixed-store multi-get path.
            for _attempt in range(3):
                rows, generation = fixed_store.get_many(
                    self.view.key_columns,
                    normalized_keys,
                    with_generation=True,
                )
                if fixed_store._read_header()["generation"] == generation:
                    break
                if _attempt == 2:
                    raise StorageError(f"fixed table changed while reading: {fixed_store.path}")
            else:  # pragma: no cover - loop either breaks or raises above
                raise StorageError(f"fixed table changed while reading: {fixed_store.path}")
        else:
            rows = self.view.get_many([public for public, _normalized in normalized])

        # A row is already detached by ``get_many``.  Avoid a second deep
        # copy for keys that occur in exactly one pipeline command, while
        # retaining independent objects when the same key is requested more
        # than once across commands.
        occurrences: dict[Any, int] = {}
        for _public, normalized_key in normalized:
            public_key = normalized_key[0] if len(normalized_key) == 1 else normalized_key
            occurrences[public_key] = occurrences.get(public_key, 0) + 1

        def result_row(public_key: Any) -> Optional[dict[str, Any]]:
            row = rows.get(public_key)
            if row is None:
                return None
            return copy.deepcopy(row) if occurrences.get(public_key, 0) > 1 else row

        results: list[Any] = []
        cursor = 0
        for kind, value in commands:
            if kind == "get_many":
                command_rows: dict[Any, dict[str, Any]] = {}
                for key in value:
                    _public, normalized_key = normalized[cursor]
                    cursor += 1
                    public_key = normalized_key[0] if len(normalized_key) == 1 else normalized_key
                    row = result_row(public_key)
                    if row is not None:
                        command_rows[public_key] = row
                results.append(command_rows)
                continue
            _public, normalized_key = normalized[cursor]
            cursor += 1
            public_key = normalized_key[0] if len(normalized_key) == 1 else normalized_key
            row = result_row(public_key)
            if kind == "contains":
                results.append(row is not None)
            else:
                results.append(row)
        return results


def _fm_writer_error(exc: BaseException, *, unknown: bool = False) -> dict[str, Any]:
    return {"error": type(exc).__name__, "message": str(exc), "unknown": bool(unknown)}


def _fm_writer_result(response: Mapping[str, Any]) -> Any:
    if "error" not in response:
        return response.get("result")
    error_type = {
        "SchemaError": SchemaError,
        "StorageBusyError": StorageBusyError,
        "StorageError": StorageError,
        "KeyError": KeyError,
        "ValueError": ValueError,
        "TypeError": TypeError,
    }.get(response.get("error"), FileMappedWriteProcessError)
    if response.get("unknown"):
        error_type = FileMappedWriteProcessError
    raise error_type(response.get("message", "file-mapped writer failed"))


class _FileMappedWriterService:
    """A single process that groups file-mapped WAL transactions."""

    def __init__(self, config: Mapping[str, Any], batch_size: int,
                 flush_interval: float, queue_size: int, request_timeout: float) -> None:
        self._config = dict(config)
        self._batch_size = int(batch_size)
        self._interval = float(flush_interval)
        self._timeout = float(request_timeout)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(int(queue_size))
        self._gate = threading.Lock()
        self._accepting = True
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._failure: Optional[dict[str, Any]] = None
        self._stats = {"commits": 0, "requests": 0, "succeeded": 0, "failed": 0,
                       "max_group": 0, "active_requests": 0}
        self._thread = threading.Thread(target=self._run, name="file-mapped-group-commit", daemon=True)
        self._thread.start()

    def _finish(self, item: dict[str, Any], response: Mapping[str, Any]) -> None:
        if item["done"].is_set():
            return
        item["response"] = dict(response)
        item["done"].set()

    def status(self) -> dict[str, Any]:
        if not self._ready.wait(self._timeout):
            return _fm_writer_error(FileMappedWriteProcessError("writer startup timed out"))
        if self._failure:
            return self._failure
        return {"result": {"pid": os.getpid(), "queued_requests": self._queue.qsize(), **self._stats}}

    def _enqueue(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._gate:
            if not self._accepting or self._failure:
                return _fm_writer_error(FileMappedWriteProcessError("writer is closed or failed; request not accepted"))
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                return _fm_writer_error(StorageBusyError("writer queue is full; request not accepted"))
        if not item["done"].wait(self._timeout):
            return _fm_writer_error(FileMappedWriteProcessError(
                f"request {item['token']} outcome unknown after timeout; do not retry blindly"), unknown=True)
        return item["response"]

    def execute(self, table: str, operation: str, payload: Any, token: str) -> dict[str, Any]:
        return self._enqueue({"table": table, "operation": operation, "payload": payload,
                              "token": token, "done": threading.Event()})

    def flush(self) -> dict[str, Any]:
        return self._enqueue({"table": None, "operation": "flush", "payload": None,
                              "token": uuid.uuid4().hex, "done": threading.Event()})

    def stop(self) -> dict[str, Any]:
        with self._gate:
            self._accepting = False
        if not self._stopped.wait(self._timeout):
            return _fm_writer_error(FileMappedWriteProcessError(
                "writer shutdown timed out; pending outcomes unknown"), unknown=True)
        return self._failure or {"result": True}

    def _commit(self, db: Any, group: Sequence[dict[str, Any]]) -> None:
        from ._file_mapped_group import FixedWriteGroup

        self._stats["active_requests"] = len(group)
        responses: dict[int, dict[str, Any]] = {}
        committing = False
        fatal = None
        try:
            # Resolve definitions before opening a table: unknown names must
            # not silently create schema-less files.
            name = group[0]["table"]
            if db.schema_registry is None or name not in db.schema_registry.names():
                raise SchemaError("group writer requires a JSON table definition")
            table = db[name]
            store = table._fixed_store
            if store is None or not store.active or not table.definition.primary_keys:
                raise SchemaError("group writer requires a fixed table with a primary key")
            with _locked(store.lock_path, timeout=store.lock_timeout):
                store._recover_locked()
                header = store._read_header()
                with store._mapped() as (_handle, mapping):
                    staged = FixedWriteGroup(table, mapping, header)
                    for index, request in enumerate(group):
                        try:
                            result = staged.apply(request["operation"], request["payload"])
                            responses[index] = {"result": result}
                        except (SchemaError, KeyError, ValueError, TypeError, OverflowError, struct.error) as exc:
                            if isinstance(exc, (OverflowError, struct.error)):
                                exc = SchemaError(str(exc))
                            responses[index] = _fm_writer_error(exc)
                # Release the mapping read permit before acquiring the write
                # permit. The existing OS/table lock stays held throughout.
                committing = True
                if staged.commit():
                    self._stats["commits"] += 1
        except BaseException as exc:
            known = not committing and isinstance(exc, (SchemaError, StorageBusyError, KeyError, ValueError, TypeError))
            response = (_fm_writer_error(exc) if known else _fm_writer_error(
                FileMappedWriteProcessError(f"write failed; outcome unknown, do not retry blindly: {exc}"), unknown=True))
            # Previously rejected requests are known not to have been staged.
            responses = {i: responses[i] if i in responses and "error" in responses[i] else response
                         for i in range(len(group))}
            if not known:
                fatal = exc
        self._stats["requests"] += len(group)
        self._stats["max_group"] = max(self._stats["max_group"], len(group))
        for index, request in enumerate(group):
            response = responses[index]
            self._stats["failed" if "error" in response else "succeeded"] += 1
            self._finish(request, response)
        self._stats["active_requests"] = 0
        if fatal is not None:
            # Never continue on a mapping/cache state after an ambiguous WAL
            # failure. Reopening a new service invokes normal WAL recovery.
            raise fatal

    def _run(self) -> None:
        pending: Optional[dict[str, Any]] = None
        current: list[dict[str, Any]] = []
        try:
            with FileMappedDatabase(**self._config) as db:
                self._ready.set()
                while True:
                    if pending is not None:
                        request, pending = pending, None
                    else:
                        try:
                            request = self._queue.get(timeout=0.01)
                        except queue.Empty:
                            with self._gate:
                                if not self._accepting and self._queue.empty():
                                    break
                            continue
                    if request["operation"] == "flush":
                        self._finish(request, {"result": True})
                        continue
                    current = [request]
                    deadline = time.perf_counter() + self._interval
                    while len(current) < self._batch_size:
                        try:
                            candidate = self._queue.get_nowait()
                        except queue.Empty:
                            remaining = deadline - time.perf_counter()
                            if remaining <= 0:
                                break
                            time.sleep(min(remaining, 0.00025))
                            continue
                        if candidate["operation"] != "flush" and candidate["table"] == request["table"]:
                            current.append(candidate)
                        else:
                            pending = candidate
                            break
                    self._commit(db, current)
                    current = []
        except BaseException as exc:
            self._failure = _fm_writer_error(
                FileMappedWriteProcessError(f"writer failed; pending outcomes unknown: {exc}"), unknown=True)
        finally:
            with self._gate:
                self._accepting = False
            for item in current + ([pending] if pending else []):
                self._finish(item, self._failure or _fm_writer_error(FileMappedWriteProcessError("writer stopped")))
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._finish(item, self._failure or _fm_writer_error(FileMappedWriteProcessError("writer stopped")))
            self._ready.set()
            self._stopped.set()


_file_mapped_writer_service: Optional[_FileMappedWriterService] = None


def _initialize_file_mapped_writer_service(*args: Any) -> None:
    global _file_mapped_writer_service
    _file_mapped_writer_service = _FileMappedWriterService(*args)


def _get_file_mapped_writer_service() -> _FileMappedWriterService:
    assert _file_mapped_writer_service is not None
    return _file_mapped_writer_service


class _FileMappedWriterManager(BaseManager):
    pass


_FileMappedWriterManager.register("writer", callable=_get_file_mapped_writer_service,
                                  exposed=("execute", "flush", "status", "stop"))


class FileMappedWriteClient:
    """Pickle-friendly authenticated endpoint for a dedicated file writer."""

    def __init__(self, address: Any, authkey: bytes) -> None:
        self._address, self._authkey = address, bytes(authkey)
        self._local = threading.local()

    def __getstate__(self) -> tuple[Any, bytes]:
        return self._address, self._authkey

    def __setstate__(self, state: tuple[Any, bytes]) -> None:
        self.__init__(*state)

    def _proxy(self) -> Any:
        if getattr(self._local, "pid", None) != os.getpid():
            manager = _FileMappedWriterManager(address=self._address, authkey=self._authkey)
            manager.connect()
            self._local.manager, self._local.proxy, self._local.pid = manager, manager.writer(), os.getpid()
        return self._local.proxy

    def _call(self, method: str, *args: Any) -> Any:
        try:
            response = getattr(self._proxy(), method)(*args)
        except (EOFError, OSError) as exc:
            FileMappedWriteClient.close(self)
            raise FileMappedWriteProcessError("writer connection lost; outcome unknown, do not retry blindly") from exc
        return _fm_writer_result(response)

    def _request(self, table: str, operation: str, payload: Any) -> Any:
        return self._call("execute", _validate_identifier(table, label="table name"), operation, payload, uuid.uuid4().hex)

    def connect(self) -> "FileMappedWriteClient":
        self._call("status")
        return self

    def append(self, table: str, row: Mapping[str, Any]) -> int:
        return int(self.insert_many(table, [row]))

    def insert_many(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        return int(self._request(table, "insert", list(rows)))

    def update(self, table: str, values: Mapping[str, Any], where: Any = None, *, limit: Optional[int] = None) -> int:
        return int(self._request(table, "update", (dict(values), where, limit)))

    def update_by_pk(self, table: str, key: Any, values: Mapping[str, Any]) -> int:
        return int(self._request(table, "update_pk", (key, dict(values))))

    def update_by_key(self, table: str, key: Any, values: Mapping[str, Any], key_column: Optional[str] = None) -> int:
        return int(self._request(table, "update_key", (key, dict(values), key_column)))

    def update_many(self, table: str, updates: Mapping[Any, Mapping[str, Any]] | Iterable[tuple[Any, Mapping[str, Any]]], *, key_column: Optional[str] = None) -> int:
        items = list(updates.items() if isinstance(updates, Mapping) else updates)
        return int(self._request(table, "update_many_pk" if key_column is None else "update_many_key",
                                 items if key_column is None else (items, key_column)))

    def delete(self, table: str, where: Any = None, *, limit: Optional[int] = None) -> int:
        return int(self._request(table, "delete", (where, limit)))

    def delete_by_pk(self, table: str, key: Any) -> int:
        return int(self._request(table, "delete_pk", key))

    def delete_by_key(self, table: str, key: Any, key_column: Optional[str] = None) -> int:
        return int(self._request(table, "delete_key", (key, key_column)))

    def delete_many(self, table: str, keys: Iterable[Any], *, key_column: Optional[str] = None) -> int:
        values = list(keys)
        return int(self._request(table, "delete_many_pk" if key_column is None else "delete_many_key",
                                 values if key_column is None else (values, key_column)))

    def flush(self) -> bool:
        return bool(self._call("flush"))

    def stats(self) -> dict[str, Any]:
        return dict(self._call("status"))

    def close(self) -> None:
        self._local.__dict__.clear()


class FileMappedWriteProcess(FileMappedWriteClient):
    """One WAL writer process that groups requests from many producers."""

    def __init__(self, directory: Union[str, os.PathLike[str]], *, schema_directory: Optional[Union[str, os.PathLike[str]]] = None,
                 lock_timeout: Optional[float] = 30.0, batch_size: int = 256,
                 flush_interval: float = 0.001, queue_size: int = 1024,
                 request_timeout: float = 30.0, start_method: str = "spawn",
                 fixed_max_records: int = _FIXED_DEFAULT_MAX_RECORDS,
                 fixed_text_bytes: int = _FIXED_DEFAULT_TEXT_BYTES,
                 fixed_object_bytes: int = _FIXED_DEFAULT_OBJECT_BYTES) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (batch_size, queue_size)):
            raise ValueError("batch_size and queue_size must be positive integers")
        try:
            valid_timeouts = (math.isfinite(float(flush_interval)) and float(flush_interval) >= 0 and
                              math.isfinite(float(request_timeout)) and float(request_timeout) > 0 and
                              (lock_timeout is None or (math.isfinite(float(lock_timeout)) and float(lock_timeout) > 0)))
        except (TypeError, ValueError):
            valid_timeouts = False
        if not valid_timeouts:
            raise ValueError("invalid file-mapped writer timeout configuration")
        if int(fixed_max_records) < 1 or int(fixed_text_bytes) < 1 or int(fixed_object_bytes) < 1024:
            raise ValueError("fixed storage settings are too small")
        root = Path(directory).expanduser().resolve()
        schema = Path(schema_directory).expanduser().resolve() if schema_directory is not None else None
        lock_value = None if lock_timeout is None else float(lock_timeout)
        self._config = {"directory": str(root), "schema_directory": str(schema) if schema else None,
                        "lock_timeout": lock_value,
                        "fixed_max_records": int(fixed_max_records),
                        "fixed_text_bytes": int(fixed_text_bytes),
                        "fixed_object_bytes": int(fixed_object_bytes)}
        self._options = (int(batch_size), float(flush_interval), int(queue_size), float(request_timeout))
        self._owner = _FileMappedWriterManager(authkey=os.urandom(32), ctx=mp.get_context(start_method))
        self._owner_pid, self._started, self._closed = os.getpid(), False, False
        self._lifecycle = threading.RLock()

    def __getstate__(self) -> Any:
        raise TypeError("pass writer.client() to workers, not the process owner")

    def _check_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise FileMappedWriteProcessError("writer owner belongs to another process; pass client()")

    def start(self) -> "FileMappedWriteProcess":
        self._check_owner()
        with self._lifecycle:
            if self._closed:
                raise FileMappedWriteProcessError("writer is closed")
            if not self._started:
                self._owner.start(initializer=_initialize_file_mapped_writer_service,
                                  initargs=(self._config, *self._options))
                FileMappedWriteClient.__init__(self, self._owner.address, self._owner._authkey)
                try:
                    FileMappedWriteClient._call(self, "status")
                except BaseException:
                    self._owner.shutdown()
                    self._closed = True
                    raise
                self._started = True
        return self

    def _call(self, method: str, *args: Any) -> Any:
        self.start()
        return FileMappedWriteClient._call(self, method, *args)

    @property
    def pid(self) -> Optional[int]:
        return self._owner._process.pid if self._started else None

    def client(self) -> FileMappedWriteClient:
        self.start()
        return FileMappedWriteClient(self._address, self._authkey)

    def close(self) -> None:
        self._check_owner()
        with self._lifecycle:
            if self._closed:
                return
            try:
                if self._started:
                    FileMappedWriteClient._call(self, "stop")
            finally:
                self._closed = True
                if self._started:
                    self._owner.shutdown()
                    FileMappedWriteClient.close(self)

    def __enter__(self) -> "FileMappedWriteProcess":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class FileMappedDatabase:
    """Collection of file-mapped tables rooted at one directory."""

    def __init__(
        self,
        directory: Union[str, os.PathLike[str]],
        *,
        schema_directory: Optional[Union[str, os.PathLike[str]]] = None,
        lock_timeout: Optional[float] = 30.0,
        fixed_max_records: int = _FIXED_DEFAULT_MAX_RECORDS,
        fixed_text_bytes: int = _FIXED_DEFAULT_TEXT_BYTES,
        fixed_object_bytes: int = _FIXED_DEFAULT_OBJECT_BYTES,
    ) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._tables: dict[str, Table] = {}
        self._lock = threading.RLock()
        if lock_timeout is not None and lock_timeout <= 0:
            raise ValueError("lock_timeout must be positive or None")
        self.lock_timeout = lock_timeout
        self.fixed_max_records = int(fixed_max_records)
        self.fixed_text_bytes = int(fixed_text_bytes)
        self.fixed_object_bytes = int(fixed_object_bytes)
        if self.fixed_max_records < 1 or self.fixed_text_bytes < 1 or self.fixed_object_bytes < 1024:
            raise ValueError("fixed storage settings are too small")
        if schema_directory is None:
            candidate = self.directory.parent / "def" / "table"
            schema_directory = candidate if candidate.exists() else None
        self.schema_directory = Path(schema_directory).expanduser().resolve() if schema_directory is not None else None
        self.schema_registry = SchemaRegistry(self.schema_directory) if self.schema_directory is not None else None

    def table(self, name: str) -> Table:
        name = _validate_identifier(name, label="table name")
        with self._lock:
            table = self._tables.get(name)
            if table is None:
                definition = None
                if self.schema_registry is not None and name in self.schema_registry.names():
                    definition = self.schema_registry.load(name)
                table = Table(
                    self.directory / f"{name}.table",
                    name=name,
                    definition=definition,
                    lock_timeout=self.lock_timeout,
                    fixed_max_records=self.fixed_max_records,
                    fixed_text_bytes=self.fixed_text_bytes,
                    fixed_object_bytes=self.fixed_object_bytes,
                )
                self._tables[name] = table
            return table

    def __getitem__(self, name: str) -> Table:
        return self.table(name)

    def execute(self, statement: str) -> QueryResult:
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError("statement must be a non-empty string")
        clean = statement.strip().rstrip(";").strip()
        keyword = clean.split(None, 1)[0].upper()
        patterns = {
            "SELECT": rf"^SELECT\b.+?\bFROM\s+(?P<table>{_IDENTIFIER})",
            "INSERT": rf"^INSERT\s+INTO\s+(?P<table>{_IDENTIFIER})",
            "UPDATE": rf"^UPDATE\s+(?P<table>{_IDENTIFIER})",
            "DELETE": rf"^DELETE\s+FROM\s+(?P<table>{_IDENTIFIER})",
        }
        pattern = patterns.get(keyword)
        if pattern is None:
            raise ValueError(f"unsupported SQL statement: {keyword}")
        match = re.search(pattern, clean, re.IGNORECASE | re.DOTALL)
        if match is None:
            raise ValueError("could not determine target table")
        return self.table(_parse_table_name(match.group("table"))).execute(clean)

    def write_process(self, **options: Any) -> "FileMappedWriteProcess":
        """Create a dedicated process that groups WAL writes for this root."""
        config = dict(directory=self.directory, schema_directory=self.schema_directory,
                      lock_timeout=self.lock_timeout,
                      fixed_max_records=self.fixed_max_records,
                      fixed_text_bytes=self.fixed_text_bytes,
                      fixed_object_bytes=self.fixed_object_bytes)
        config.update(options)
        return FileMappedWriteProcess(**config)

    def close(self) -> None:
        """Release in-process caches; table files remain available to other processes."""
        with self._lock:
            for table in self._tables.values():
                table._close()
            self._tables.clear()

    def refresh_schema(self) -> None:
        """Reload JSON table definitions and rebind already opened tables."""

        with self._lock:
            if self.schema_registry is not None:
                self.schema_registry.clear()
            self._tables.clear()

    def __enter__(self) -> "FileMappedDatabase":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
