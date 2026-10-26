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
readers take the same file lock as writers so a mapped snapshot is never read
while it is being replaced.  The in-process snapshot cache avoids reparsing an
unchanged file on hot read paths and is invalidated by the file's mtime/size.
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
    "StorageError",
    "Table",
]


class StorageError(RuntimeError):
    """Raised when a table file is missing, truncated or otherwise corrupt."""


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
    """A blocking advisory lock that works for both threads and processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
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
                    time.sleep(0.01)
        else:  # pragma: no cover - selected on POSIX only
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
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
def _locked(lock_path: Path) -> Iterator[None]:
    """Hold both the local and inter-process lock for one table operation."""

    with _thread_lock(lock_path):
        with _FileLock(lock_path):
            yield


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
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._cache: Optional[_Snapshot] = None
        self._ensure_exists()

    def _ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.lock_path):
            if not self.path.exists():
                self._write_snapshot_locked([], [], 0)

    def _read_snapshot_locked(self) -> _Snapshot:
        if not self.path.exists():
            self._write_snapshot_locked([], [], 0)
        try:
            stat = self.path.stat()
            cache_key = (stat.st_mtime_ns, stat.st_size)
            if self._cache is not None and self._cache.cache_key == cache_key:
                return self._cache
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
            snapshot = _Snapshot(normalised_rows, normalised_columns, int(generation), cache_key)
            self._cache = snapshot
            return snapshot
        except StorageError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError(f"cannot read table {self.path}: {exc}") from exc

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
            os.replace(temporary, self.path)
            temporary = None
            if os.name != "nt":  # pragma: no cover - POSIX durability detail
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            stat = self.path.stat()
            self._cache = _Snapshot(copy.deepcopy(rows), list(columns), generation, (stat.st_mtime_ns, stat.st_size))
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
        result = dict(row)
        for key in result:
            _validate_identifier(str(key), label="column name")
        # Validate JSON compatibility before taking the table lock for a write.
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"row contains a non-JSON value: {exc}") from exc
        return result

    @staticmethod
    def _columns_after(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> list[str]:
        result = list(columns)
        for row in rows:
            for key in row:
                key = str(key)
                if key not in result:
                    result.append(key)
        return result

    @contextmanager
    def _transaction(self) -> Iterator[tuple[list[dict[str, Any]], list[str], int]]:
        with _locked(self.lock_path):
            snapshot = self._read_snapshot_locked()
            rows = copy.deepcopy(snapshot.rows)
            columns = list(snapshot.columns)
            yield rows, columns, snapshot.generation

    def _mutate(self, function: Callable[[list[dict[str, Any]], list[str]], Any]) -> Any:
        with self._transaction() as (rows, columns, generation):
            result = function(rows, columns)
            columns = self._columns_after(rows, columns)
            if self.definition is not None:
                self.definition.validate_rows(rows)
            self._write_snapshot_locked(rows, columns, generation + 1)
            return result

    def _read_rows(self) -> list[dict[str, Any]]:
        with _locked(self.lock_path):
            return copy.deepcopy(self._read_snapshot_locked().rows)

    @property
    def columns(self) -> list[str]:
        """Return the table's known columns in first-seen order."""

        return self._read_snapshot_columns()

    @property
    def schema(self) -> Optional[TableDefinition]:
        """Return the loaded JSON definition, if this table has one."""

        return self.definition

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
        with _locked(self.lock_path):
            # Do not expose cached row dictionaries to a user-supplied
            # predicate.  A callable is allowed to inspect a row and may
            # mutate its argument; that must never mutate the read cache.
            rows = [row for row in copy.deepcopy(self._read_snapshot_locked().rows) if predicate(row)]
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
        with _locked(self.lock_path):
            return sum(1 for row in copy.deepcopy(self._read_snapshot_locked().rows) if predicate(row))

    def insert_one(self, row: Mapping[str, Any]) -> int:
        row = self._normalise_row(row)

        def add(rows: list[dict[str, Any]], columns: list[str]) -> int:
            index = len(rows)
            rows.append(row)
            return index

        return int(self._mutate(add))

    def insert_many(self, rows: Iterable[Mapping[str, Any]]) -> int:
        values = [self._normalise_row(row) for row in rows]

        def add(current: list[dict[str, Any]], columns: list[str]) -> int:
            current.extend(values)
            return len(values)

        return int(self._mutate(add))

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

        def apply(rows: list[dict[str, Any]], columns: list[str]) -> int:
            changed = 0
            for row in rows:
                if predicate(row) and (limit is None or changed < limit):
                    row.update(copy.deepcopy(patch))
                    changed += 1
            return changed

        return int(self._mutate(apply))

    def delete(
        self,
        where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]] = None,
        *,
        limit: Optional[int] = None,
    ) -> int:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        predicate = _compile_where(where)

        def remove(rows: list[dict[str, Any]], columns: list[str]) -> int:
            removed = 0
            kept: list[dict[str, Any]] = []
            for row in rows:
                if predicate(row) and (limit is None or removed < limit):
                    removed += 1
                else:
                    kept.append(row)
            rows[:] = kept
            return removed

        return int(self._mutate(remove))

    def sort(self, *, key: Optional[str] = None, reverse: bool = False) -> None:
        """Sort the physical row order, like ``list.sort``."""

        order = None if key is None else [(key, "DESC" if reverse else "ASC")]

        def reorder(rows: list[dict[str, Any]], columns: list[str]) -> None:
            _sort_rows(rows, order)

        self._mutate(reorder)

    # ---- list-like API ---------------------------------------------------------
    def __len__(self) -> int:
        with _locked(self.lock_path):
            return len(self._read_snapshot_locked().rows)

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

        self._mutate(assign)

    def __delitem__(self, index: Union[int, slice]) -> None:
        def remove(rows: list[dict[str, Any]], columns: list[str]) -> None:
            del rows[index]

        self._mutate(remove)

    def insert(self, index: int, value: Mapping[str, Any]) -> None:
        row = self._normalise_row(value)

        def add(rows: list[dict[str, Any]], columns: list[str]) -> None:
            rows.insert(index, row)

        self._mutate(add)

    def append(self, value: Mapping[str, Any]) -> None:
        self.insert_one(value)

    def extend(self, values: Iterable[Mapping[str, Any]]) -> None:
        self.insert_many(values)

    def pop(self, index: int = -1) -> dict[str, Any]:
        def remove(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, Any]:
            return rows.pop(index)

        return copy.deepcopy(self._mutate(remove))

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

        first, count = self._mutate(add)
        return QueryResult(rowcount=count, lastrowid=(first if count else None))

    def _read_snapshot_columns(self) -> list[str]:
        with _locked(self.lock_path):
            return list(self._read_snapshot_locked().columns)

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
    ) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._tables: dict[str, Table] = {}
        self._lock = threading.RLock()
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
                table = Table(self.directory / f"{name}.table", name=name, definition=definition)
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

