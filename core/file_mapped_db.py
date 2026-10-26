"""File-mapped, relation-style storage for small and medium sized tables.

The module deliberately uses only Python's standard library.  A table is stored
in one ``*.table`` file.  The file contains a small binary header followed by a
UTF-8 JSON snapshot; snapshots are read through :mod:`mmap` and replaced
atomically after a write.  A side-car ``*.lock`` file protects the complete
read/modify/write operation across processes, while a process-local ``RLock``
also makes nested and multi-threaded access safe.

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
The in-process snapshot cache avoids reparsing an unchanged file on hot read
paths and is invalidated by the file's mtime/size.
"""

from __future__ import annotations

import ast
import copy
import json
import mmap
import os
import re
import struct
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableSequence, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .table_schema import SchemaError, SchemaRegistry, TableDefinition

if os.name == "nt":  # pragma: no cover - exercised on Windows in CI/users' machines
    import msvcrt
else:  # pragma: no cover - the branch is covered on POSIX
    import fcntl


__all__ = [
    "Database",
    "FileMappedDatabase",
    "FileMappedDB",
    "MappedDatabase",
    "MappedTable",
    "QueryResult",
    "RelationTable",
    "StorageBusyError",
    "StorageError",
    "Table",
]


class StorageError(RuntimeError):
    """Raised when a table file is missing, truncated or otherwise corrupt."""


class StorageBusyError(StorageError):
    """Raised when a thread or process cannot acquire a table lock in time."""


_MAGIC = b"RTDBMAP1"  # eight bytes
_HEADER = struct.Struct(">8sQQQ")  # magic, payload length, generation, crc32
_MAX_PAYLOAD = 512 * 1024 * 1024
_IDENTIFIER = r"(?:[A-Za-z_][A-Za-z0-9_]*|`[^`]+`|\"[^\"]+\"|\[[^\]]+\])"
_IDENTIFIER_RE = re.compile(rf"^\s*(?P<name>{_IDENTIFIER})\s*$")


# One RLock per lock-file path.  The OS lock is still required: a Python lock
# cannot coordinate two independent processes.
_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[key] = lock
        return lock


class _FileLock:
    """A bounded advisory lock that works for both threads and processes."""

    def __init__(self, path: Path, timeout: Optional[float] = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self._handle: Any = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        # msvcrt.locking locks bytes starting at the current offset.  Keep one
        # byte in the side-car file and always seek to its beginning.
        if os.name == "nt":
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"0")
                self._handle.flush()
            self._handle.seek(0)
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
                        self._handle.close()
                        self._handle = None
                        raise StorageBusyError(f"timed out waiting for table lock: {self.path}")
                    time.sleep(0.002)
        else:  # pragma: no cover - selected on POSIX only
            while True:
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        self._handle.close()
                        self._handle = None
                        raise StorageBusyError(f"timed out waiting for table lock: {self.path}")
                    time.sleep(0.002)
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
            self._handle.close()
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


@dataclass(frozen=True)
class _Snapshot:
    rows: list[dict[str, Any]]
    columns: list[str]
    generation: int
    cache_key: tuple[int, int]
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
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        if self.path.suffix == "":
            self.path = self.path.with_suffix(".table")
        self.name = _validate_identifier(name or self.path.stem, label="table name")
        if definition is not None and definition.eng_name != self.name:
            raise ValueError(
                f"table definition {definition.eng_name!r} does not match table {self.name!r}"
            )
        self.definition = definition
        if lock_timeout is not None and lock_timeout <= 0:
            raise ValueError("lock_timeout must be positive or None")
        self.lock_timeout = lock_timeout
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._cache: Optional[_Snapshot] = None
        self._cache_guard = threading.RLock()
        self._ensure_exists()

    def _ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.lock_path, timeout=self.lock_timeout):
            if not self.path.exists():
                self._write_snapshot_locked([], [], 0)

    def _read_snapshot_locked(self) -> _Snapshot:
        """Read the current immutable snapshot.

        The method name is retained for the write path, but readers no longer
        need to hold the table's exclusive file lock.  Writers publish with
        ``os.replace``; the optimistic read below retries if a replacement is
        observed while the file is being mapped.
        """

        return self._read_snapshot()

    def _read_snapshot(self) -> _Snapshot:
        if not self.path.exists():
            self._write_snapshot_locked([], [], 0)
        last_error: Optional[Exception] = None
        for _attempt in range(4):
            try:
                stat = self.path.stat()
                cache_key = (stat.st_mtime_ns, stat.st_size)
                with self._cache_guard:
                    cached = self._cache
                if cached is not None and cached.cache_key == cache_key:
                    return cached
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
                after = self.path.stat()
                after_key = (after.st_mtime_ns, after.st_size)
                if after_key != cache_key:
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
                if self.definition is not None:
                    try:
                        self.definition.validate_rows(normalised_rows)
                    except SchemaError as exc:
                        raise StorageError(f"table {self.name!r} does not match its definition: {exc}") from exc
                indexes, primary_index = _build_snapshot_indexes(normalised_rows, self.definition)
                snapshot = _Snapshot(
                    normalised_rows,
                    normalised_columns,
                    int(generation),
                    cache_key,
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

    def _write_snapshot_locked(self, rows: list[dict[str, Any]], columns: list[str], generation: int) -> None:
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
            stat = self.path.stat()
            indexes, primary_index = _build_snapshot_indexes(rows, self.definition)
            snapshot = _Snapshot(rows, list(columns), generation, (stat.st_mtime_ns, stat.st_size), indexes, primary_index)
            with self._cache_guard:
                self._cache = snapshot
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
        # Validate JSON compatibility before taking the table lock for a write.
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
        if self.definition is not None and len(self.definition.primary_keys) > 1:
            lookup = key if isinstance(key, tuple) else (key,)
        else:
            lookup = (key,)
        positions = snapshot.primary_index.get(lookup)
        if not positions or len(positions) != 1:
            raise KeyError(key)
        return positions[0]

    def get_by_pk(self, key: Any) -> dict[str, Any]:
        """Read one row by the declared primary key without a table scan."""

        return copy.deepcopy(self._read_snapshot().rows[self._primary_key_position(key)])

    def update_by_pk(self, key: Any, values: Mapping[str, Any]) -> int:
        """Update one row by primary key and return the affected row count."""

        if self.definition is None:
            raise KeyError(key)
        if len(self.definition.primary_keys) == 1:
            where: Mapping[str, Any] = {self.definition.primary_keys[0]: key}
        else:
            if not isinstance(key, tuple) or len(key) != len(self.definition.primary_keys):
                raise KeyError(key)
            where = dict(zip(self.definition.primary_keys, key))
        return self.update(values, where, limit=1)

    def delete_by_pk(self, key: Any) -> int:
        """Delete one row by primary key and return the affected row count."""

        if self.definition is None:
            raise KeyError(key)
        if len(self.definition.primary_keys) == 1:
            where: Mapping[str, Any] = {self.definition.primary_keys[0]: key}
        else:
            if not isinstance(key, tuple) or len(key) != len(self.definition.primary_keys):
                raise KeyError(key)
            where = dict(zip(self.definition.primary_keys, key))
        return self.delete(where, limit=1)

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

    to_list = all

    def count(self, where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]] = None) -> int:
        predicate = _compile_where(where)
        snapshot = self._read_snapshot()
        positions = self._candidate_positions(snapshot, where)
        rows = snapshot.rows if positions is None else (snapshot.rows[position] for position in positions)
        if callable(where):
            return sum(1 for row in rows if predicate(copy.deepcopy(row)))
        return sum(1 for row in rows if predicate(row))

    def insert_one(self, row: Mapping[str, Any]) -> int:
        row = self._normalise_row(row)

        def add(rows: list[dict[str, Any]], columns: list[str]) -> int:
            index = len(rows)
            rows.append(row)
            return index

        return int(self._mutate(add, copy_rows=False, new_columns=row.keys()))

    def insert_many(self, rows: Iterable[Mapping[str, Any]]) -> int:
        values = [self._normalise_row(row) for row in rows]

        def add(current: list[dict[str, Any]], columns: list[str]) -> int:
            current.extend(values)
            return len(values)

        new_columns = {key for row in values for key in row}
        return int(self._mutate(add, copy_rows=False, new_columns=new_columns))

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

    def delete(
        self,
        where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]] = None,
        *,
        limit: Optional[int] = None,
    ) -> int:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        predicate = _compile_where(where)

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

    def sort(self, *, key: Optional[str] = None, reverse: bool = False) -> None:
        """Sort the physical row order, like ``list.sort``."""

        order = None if key is None else [(key, "DESC" if reverse else "ASC")]

        def reorder(rows: list[dict[str, Any]], columns: list[str]) -> None:
            _sort_rows(rows, order)

        self._mutate(reorder, copy_rows=False)

    # ---- list-like API ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self._read_snapshot().rows)

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

    query = execute
    execute_sql = execute

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


MappedTable = Table
RelationTable = Table


class FileMappedDatabase:
    """Collection of file-mapped tables rooted at one directory."""

    def __init__(
        self,
        directory: Union[str, os.PathLike[str]],
        *,
        schema_directory: Optional[Union[str, os.PathLike[str]]] = None,
        lock_timeout: Optional[float] = 30.0,
    ) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._tables: dict[str, Table] = {}
        self._lock = threading.RLock()
        if lock_timeout is not None and lock_timeout <= 0:
            raise ValueError("lock_timeout must be positive or None")
        self.lock_timeout = lock_timeout
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
                )
                self._tables[name] = table
            return table

    open_table = table
    create_table = table

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

    query = execute
    execute_sql = execute

    def close(self) -> None:
        """Release in-process caches; table files remain available to other processes."""
        with self._lock:
            for table in self._tables.values():
                table._cache = None
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


Database = FileMappedDatabase
FileMappedDB = FileMappedDatabase
MappedDatabase = FileMappedDatabase

