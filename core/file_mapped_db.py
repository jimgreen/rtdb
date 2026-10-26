"""File-mapped, relation-style storage for small and medium sized tables.

The module deliberately uses only Python's standard library.  A
schema-defined table is stored in one fixed-width binary ``*.table`` file;
records have stable offsets and are read or updated directly through
:mod:`mmap`.  A binary redo WAL and redundant generation headers make slot
updates crash-safe.  A side-car ``*.lock`` file protects the complete
read/modify/write operation across processes, while a process-local ``RLock``
also makes nested and multi-threaded access safe.  Definition-less legacy
tables retain the old UTF-8 JSON snapshot reader for compatibility.

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
import copy
import json
import mmap
import os
import pickle
import re
import struct
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, MutableSequence, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .table_schema import ColumnDefinition, SchemaError, SchemaRegistry, TableDefinition, TableHead

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
    "TableKeyValue",
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
# stay memory mapped and be updated in place.  The old RTDBMAP1/JSON format is
# still read for legacy, definition-less files; newly created defined tables
# never pass through JSON.
_FIXED_MAGIC = b"RTDBFIX2"
_FIXED_VERSION = 1
_FIXED_HEADER_BODY = struct.Struct(">8sHHQQQQII")
_FIXED_HEADER_CRC = struct.Struct(">I")
_FIXED_HEADER_SLOT = 128
_FIXED_DATA_OFFSET = 4096
_FIXED_SCHEMA_MAGIC = b"RTDBSCM1"
_FIXED_SCHEMA_HEADER = struct.Struct(">8sH")
_FIXED_SCHEMA_FIELD = struct.Struct(">HHBQ")
_FIXED_SLOT_HEADER = struct.Struct(">BQI")
_FIXED_WAL_MAGIC = b"RTDBFWL2"
_FIXED_WAL_HEADER = struct.Struct(">8sQI")
_FIXED_WAL_END_MAGIC = b"RTDBFEND"
_FIXED_WAL_TRAILER = struct.Struct(">8sQ")
_FIXED_WAL_THRESHOLD = 4 * 1024 * 1024
_FIXED_WAL_PAYLOAD_HEADER = struct.Struct(">QQI")
_FIXED_WAL_ENTRY_HEADER = struct.Struct(">QI")


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


def _fixed_fields(definition: TableDefinition) -> tuple[tuple[_FixedField, ...], int, int]:
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
            payload_width = max(1, int(column.data_length or 64)) * 4
        else:
            payload_width = max(1024, int(column.data_length or 256) * 4)
        width = 1 + payload_width  # null marker plus fixed payload
        fields.append(_FixedField(column.eng_name, kind, width, offset))
        descriptor.extend(column.eng_name.encode("utf-8"))
        descriptor.extend(b"\0")
        descriptor.extend(kind.encode("ascii"))
        descriptor.extend(struct.pack(">Q", width))
        offset += width
    layout_crc = zlib.crc32(descriptor) & 0xFFFFFFFF
    return tuple(fields), offset, layout_crc


def _fixed_schema_blob(definition: TableDefinition) -> bytes:
    payload = bytearray(_FIXED_SCHEMA_HEADER.pack(_FIXED_SCHEMA_MAGIC, len(definition.columns)))
    for column in definition.columns:
        name = column.eng_name.encode("utf-8")
        data_type = column.data_type.encode("utf-8")
        if len(name) > 0xFFFF or len(data_type) > 0xFFFF:
            raise SchemaError(f"fixed schema name is too long: {column.eng_name!r}")
        payload.extend(_FIXED_SCHEMA_FIELD.pack(len(name), len(data_type), 1 if column.primary_key else 0, int(column.data_length)))
        payload.extend(name)
        payload.extend(data_type)
    if len(payload) > _FIXED_DATA_OFFSET - _FIXED_HEADER_SLOT * 2:
        raise SchemaError(f"fixed schema metadata is too large: {definition.eng_name!r}")
    return bytes(payload)


def _load_fixed_definition(path: Path, table_name: str) -> TableDefinition:
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
    for _ in range(count):
        if cursor + _FIXED_SCHEMA_FIELD.size > len(raw):
            raise StorageError(f"fixed schema metadata is truncated: {path}")
        name_length, type_length, primary, data_length = _FIXED_SCHEMA_FIELD.unpack(raw[cursor : cursor + _FIXED_SCHEMA_FIELD.size])
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
        columns.append(
            ColumnDefinition(
                chn_name=name,
                eng_name=name,
                primary_key=bool(primary),
                data_type=data_type,
                data_length=int(data_length),
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
    head = TableHead(chn_name=table_name, eng_name=table_name, max_elements=int(max_records))
    return TableDefinition(head=head, columns=tuple(columns), path=path)


class _FixedRecordStore:
    """Direct mmap store for schema-defined fixed-width records.

    The row area is preallocated from ``head.max_elements`` and each slot has
    a stable offset.  A key lookup maps directly to a slot and decodes only the
    requested row.  Writes use a compact binary redo WAL, then publish a
    redundant generation header.  No JSON is involved in the base file or in
    this store's WAL.
    """

    def __init__(self, path: Path, definition: TableDefinition, lock_path: Path, lock_timeout: Optional[float]) -> None:
        self.path = path
        self.definition = definition
        self.lock_path = lock_path
        self.lock_timeout = lock_timeout
        self.fields, self.payload_size, self.layout_crc = _fixed_fields(definition)
        self.record_size = _FIXED_SLOT_HEADER.size + self.payload_size
        self.max_records = int(definition.max_elements)
        self.wal_path = path.with_suffix(path.suffix + ".fixedwal")
        self.active = False
        self._cache_guard = threading.RLock()
        self._snapshot_cache: Optional[_Snapshot] = None
        self._key_cache: dict[tuple[str, ...], tuple[int, dict[tuple[Any, ...], tuple[int, ...]]]] = {}
        self._row_cache: dict[tuple[tuple[str, ...], tuple[Any, ...]], tuple[int, dict[str, Any]]] = {}
        self._header_cache: Optional[tuple[tuple[int, ...], dict[str, int]]] = None

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
            self.active = True
            self._read_header()
            self._recover_locked()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_file_locked(generation=0, row_count=0)
        self.active = True

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
        signature = _file_signature(self.path)
        with self._cache_guard:
            if self._header_cache is not None and self._header_cache[0] == signature:
                return self._header_cache[1]
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(_FIXED_DATA_OFFSET)
        except OSError as exc:
            raise StorageError(f"cannot read fixed table {self.path}: {exc}") from exc
        candidates = [
            self._decode_header_slot(raw[:_FIXED_HEADER_SLOT]),
            self._decode_header_slot(raw[_FIXED_HEADER_SLOT:_FIXED_DATA_OFFSET]),
        ]
        valid = [candidate for candidate in candidates if candidate is not None]
        if not valid:
            raise StorageError(f"fixed table headers are corrupt: {self.path}")
        header = max(valid, key=lambda item: item["generation"])
        with self._cache_guard:
            self._header_cache = (signature, header)
        return header

    def close(self) -> None:
        with self._cache_guard:
            self._snapshot_cache = None
            self._key_cache.clear()
            self._row_cache.clear()

    def _create_file_locked(self, *, generation: int, row_count: int) -> None:
        temporary: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile("w+b", prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, delete=False) as handle:
                temporary = handle.name
                slot = self._header_slot(self._header_body(generation, row_count))
                handle.write(slot)
                handle.write(slot)
                handle.write(_fixed_schema_blob(self.definition))
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

    @contextmanager
    def _mapped(self, *, write: bool = False) -> Iterator[tuple[Any, mmap.mmap]]:
        try:
            handle = self.path.open("r+b" if write else "rb")
            mapping = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_WRITE if write else mmap.ACCESS_READ)
        except (OSError, ValueError) as exc:
            raise StorageError(f"cannot map fixed table {self.path}: {exc}") from exc
        try:
            yield handle, mapping
        finally:
            mapping.close()
            handle.close()

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

    def _decode_field(self, raw: bytes, field: _FixedField) -> Any:
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
            return payload.rstrip(b"\0").decode("utf-8")
        if len(payload) < 4:
            raise StorageError(f"invalid fixed object field {field.name!r}: {self.path}")
        length = struct.unpack(">I", payload[:4])[0]
        if length > len(payload) - 4:
            raise StorageError(f"invalid fixed object length {field.name!r}: {self.path}")
        try:
            return pickle.loads(payload[4 : 4 + length])
        except (EOFError, pickle.UnpicklingError, ValueError) as exc:
            raise StorageError(f"cannot decode fixed object field {field.name!r}: {self.path}") from exc

    def _encode_payload(self, row: Mapping[str, Any]) -> bytes:
        payload = bytearray(self.payload_size)
        for field in self.fields:
            encoded = self._encode_field(row.get(field.name), field)
            payload[field.offset : field.offset + field.width] = encoded
        return bytes(payload)

    def _decode_payload(self, payload: bytes, *, fields: Optional[Sequence[_FixedField]] = None) -> dict[str, Any]:
        selected = self.fields if fields is None else fields
        result: dict[str, Any] = {}
        for field in selected:
            result[field.name] = self._decode_field(payload[field.offset : field.offset + field.width], field)
        return result

    def _encode_record(self, row: Mapping[str, Any], order: int) -> bytes:
        payload = self._encode_payload(row)
        return _FIXED_SLOT_HEADER.pack(1, int(order), zlib.crc32(payload) & 0xFFFFFFFF) + payload

    def _decode_record(self, raw: bytes, *, fields: Optional[Sequence[_FixedField]] = None) -> tuple[int, Optional[dict[str, Any]]]:
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
        return self._decode_record(mapping[offset : offset + self.record_size], fields=fields)

    def _wal_frame(self, generation: int, row_count: int, entries: Sequence[tuple[int, bytes]]) -> bytes:
        payload = bytearray(_FIXED_WAL_PAYLOAD_HEADER.pack(generation, row_count, len(entries)))
        for slot, record in entries:
            if len(record) != self.record_size:
                raise StorageError(f"invalid fixed WAL record size: {self.path}")
            payload.extend(_FIXED_WAL_ENTRY_HEADER.pack(slot, len(record)))
            payload.extend(record)
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        return _FIXED_WAL_HEADER.pack(_FIXED_WAL_MAGIC, len(payload), checksum) + payload + _FIXED_WAL_TRAILER.pack(_FIXED_WAL_END_MAGIC, len(payload))

    def _append_wal_locked(self, generation: int, row_count: int, entries: Sequence[tuple[int, bytes]]) -> None:
        frame = self._wal_frame(generation, row_count, entries)
        try:
            with self.wal_path.open("ab") as handle:
                handle.write(frame)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageError(f"cannot append fixed WAL {self.wal_path}: {exc}") from exc

    def _read_wal(self) -> tuple[list[tuple[int, int, list[tuple[int, bytes]]]], int, bool]:
        if not self.wal_path.exists():
            return [], 0, False
        records: list[tuple[int, int, list[tuple[int, bytes]]]] = []
        valid_end = 0
        incomplete = False
        try:
            with self.wal_path.open("rb") as handle:
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
        return records, valid_end, incomplete

    def _truncate_wal_locked(self, length: int = 0, *, sync: bool = True) -> None:
        if not self.wal_path.exists():
            return
        try:
            with self.wal_path.open("r+b") as handle:
                handle.truncate(length)
                handle.flush()
                if sync:
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageError(f"cannot truncate fixed WAL {self.wal_path}: {exc}") from exc

    def _write_header_locked(self, generation: int, row_count: int) -> None:
        slot = self._header_slot(self._header_body(generation, row_count))
        offset = (generation & 1) * _FIXED_HEADER_SLOT
        try:
            with self.path.open("r+b") as handle:
                handle.seek(offset)
                handle.write(slot)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageError(f"cannot publish fixed table header {self.path}: {exc}") from exc
        with self._cache_guard:
            self._header_cache = None

    def _apply_entries_locked(self, entries: Sequence[tuple[int, bytes]]) -> None:
        with self._mapped(write=True) as (handle, mapping):
            for slot, record in entries:
                offset = self._slot_offset(slot)
                mapping[offset : offset + self.record_size] = record
            mapping.flush()
            handle.flush()
            os.fsync(handle.fileno())

    def _recover_locked(self) -> None:
        if not self.active or not self.wal_path.exists():
            return
        records, valid_end, incomplete = self._read_wal()
        if not records and not incomplete:
            return
        header = self._read_header()
        changed = False
        for generation, row_count, entries in records:
            if generation <= header["generation"]:
                continue
            if generation != header["generation"] + 1:
                raise StorageError(f"fixed WAL sequence gap: {self.wal_path}")
            self._apply_entries_locked(entries)
            self._write_header_locked(generation, row_count)
            header = {"generation": generation, "row_count": row_count}
            changed = True
        self._truncate_wal_locked(0 if incomplete or records else valid_end)
        if changed:
            with self._cache_guard:
                self._snapshot_cache = None
                self._key_cache.clear()
                self._row_cache.clear()

    def _recover_if_needed(self) -> None:
        if self.wal_path.exists() and self.wal_path.stat().st_size:
            with _locked(self.lock_path, timeout=self.lock_timeout):
                self._recover_locked()

    def _build_key_index_from_map(self, mapping: mmap.mmap, key_columns: Sequence[str]) -> dict[tuple[Any, ...], tuple[int, ...]]:
        fields = tuple(field for field in self.fields if field.name in key_columns)
        if len(fields) != len(key_columns):
            raise KeyError(next(column for column in key_columns if column not in {field.name for field in self.fields}))
        index: dict[tuple[Any, ...], list[int]] = {}
        for slot in range(self.max_records):
            order, row = self._read_record(mapping, slot, fields=fields)
            if row is None:
                continue
            key = tuple(row[column] for column in key_columns)
            try:
                hash(key)
            except TypeError as exc:
                raise StorageError(f"unhashable fixed key in {self.path}") from exc
            index.setdefault(key, []).append(slot)
        return {key: tuple(slots) for key, slots in index.items()}

    def _key_index(self, key_columns: Sequence[str], *, header: Optional[dict[str, int]] = None) -> dict[tuple[Any, ...], tuple[int, ...]]:
        self._recover_if_needed()
        key = tuple(key_columns)
        if header is None:
            header = self._read_header()
        with self._cache_guard:
            cached = self._key_cache.get(key)
            if cached is not None and cached[0] == header["generation"]:
                return cached[1]
        with self._mapped() as (_handle, mapping):
            built = self._build_key_index_from_map(mapping, key)
        with self._cache_guard:
            self._key_cache[key] = (header["generation"], built)
        return built

    def _key_index_locked(self, mapping: mmap.mmap, generation: int, key_columns: Sequence[str]) -> dict[tuple[Any, ...], tuple[int, ...]]:
        key = tuple(key_columns)
        with self._cache_guard:
            cached = self._key_cache.get(key)
            if cached is not None and cached[0] == generation:
                return cached[1]
        built = self._build_key_index_from_map(mapping, key)
        with self._cache_guard:
            self._key_cache[key] = (generation, built)
        return built

    def _find_slot(self, key_columns: Sequence[str], key: tuple[Any, ...], index: Optional[dict[tuple[Any, ...], tuple[int, ...]]] = None) -> int:
        positions = (self._key_index(key_columns) if index is None else index).get(key, ())
        if not positions:
            raise KeyError(key[0] if len(key) == 1 else key)
        if len(positions) != 1:
            raise ValueError(f"key {key[0] if len(key) == 1 else key!r} is not unique in fixed table {self.path}")
        return positions[0]

    def _free_slot(self, mapping: mmap.mmap, preferred: int = 0) -> int:
        """Find a free slot, starting at the expected append position."""

        preferred = max(0, min(int(preferred), self.max_records - 1)) if self.max_records else 0
        for slot in range(preferred, self.max_records):
            if mapping[self._slot_offset(slot)] == 0:
                return slot
        for slot in range(0, preferred):
            if mapping[self._slot_offset(slot)] == 0:
                return slot
        raise SchemaError(f"table {self.definition.eng_name!r} reached max_elements {self.max_records}")

    def _commit_locked(
        self,
        entries: Sequence[tuple[int, bytes]],
        row_count: int,
        generation: Optional[int] = None,
        *,
        preserve_key_cache: bool = False,
        key_cache_additions: Optional[tuple[tuple[str, ...], Sequence[tuple[tuple[Any, ...], int]]]] = None,
    ) -> None:
        self._recover_locked()
        header = self._read_header()
        next_generation = header["generation"] + 1 if generation is None else generation
        self._append_wal_locked(next_generation, row_count, entries)
        self._apply_entries_locked(entries)
        self._write_header_locked(next_generation, row_count)
        try:
            # The durable header is already published.  A leftover frame has
            # sequence <= header generation and is therefore harmless after a
            # crash, so truncate it without a fourth fsync on every write.
            if self.wal_path.stat().st_size >= _FIXED_WAL_THRESHOLD:
                self._truncate_wal_locked()
            else:
                self._truncate_wal_locked(sync=False)
        except OSError as exc:
            raise StorageError(f"cannot inspect fixed WAL {self.wal_path}: {exc}") from exc
        with self._cache_guard:
            self._snapshot_cache = None
            self._row_cache.clear()
            if preserve_key_cache:
                updated_cache: dict[tuple[str, ...], tuple[int, dict[tuple[Any, ...], tuple[int, ...]]]] = {}
                additions_columns = key_cache_additions[0] if key_cache_additions is not None else None
                additions = dict(key_cache_additions[1]) if key_cache_additions is not None else {}
                for cache_key, (_old_generation, index) in self._key_cache.items():
                    updated_index = index
                    if additions_columns is not None and cache_key == additions_columns and additions:
                        updated_index = dict(index)
                        for key, slot in additions.items():
                            updated_index[key] = tuple(updated_index.get(key, ())) + (slot,)
                    updated_cache[cache_key] = (next_generation, updated_index)
                self._key_cache = updated_cache
            else:
                self._key_cache.clear()

    def _row_for_slot(self, mapping: mmap.mmap, slot: int) -> dict[str, Any]:
        _order, row = self._read_record(mapping, slot)
        if row is None:
            raise KeyError(slot)
        return row

    def get(self, key_columns: Sequence[str], key: tuple[Any, ...]) -> dict[str, Any]:
        for _attempt in range(3):
            self._recover_if_needed()
            header = self._read_header()
            cache_key = (tuple(key_columns), key)
            with self._cache_guard:
                cached_row = self._row_cache.get(cache_key)
            if cached_row is not None and cached_row[0] == header["generation"]:
                return copy.deepcopy(cached_row[1])
            index = self._key_index(key_columns, header=header)
            slot = self._find_slot(key_columns, key, index)
            try:
                with self._mapped() as (_handle, mapping):
                    row = self._row_for_slot(mapping, slot)
                if tuple(row.get(column) for column in key_columns) == key:
                    with self._cache_guard:
                        self._row_cache[(tuple(key_columns), key)] = (header["generation"], row)
                    return copy.deepcopy(row)
                # A concurrent delete/reinsert reused the old slot. Rebuild
                # the index on the next attempt.
            except StorageError:
                if _attempt == 2:
                    raise
                time.sleep(0.001 * (_attempt + 1))
        raise StorageError(f"fixed table changed while reading: {self.path}")

    def get_many(self, key_columns: Sequence[str], keys: Sequence[tuple[Any, ...]]) -> dict[Any, dict[str, Any]]:
        self._recover_if_needed()
        index = self._key_index(key_columns)
        with self._mapped() as (_handle, mapping):
            result: dict[Any, dict[str, Any]] = {}
            for key in keys:
                positions = index.get(key, ())
                if not positions:
                    continue
                if len(positions) != 1:
                    raise ValueError(f"key {key!r} is not unique in fixed table {self.path}")
                result[key[0] if len(key) == 1 else key] = copy.deepcopy(self._row_for_slot(mapping, positions[0]))
            return result

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

    def update(self, key_columns: Sequence[str], key: tuple[Any, ...], patch: Mapping[str, Any], *, upsert: bool = False) -> int:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            with self._mapped() as (_handle, mapping):
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
                elif len(positions) != 1:
                    raise ValueError(f"key {key!r} is not unique in fixed table {self.path}")
                else:
                    slot = positions[0]
                    order, current = self._read_record(mapping, slot)
                    assert current is not None
                    updated = dict(current)
                    updated.update(copy.deepcopy(dict(patch)))
                    self._validate_row(updated)
                    updated_key = tuple(updated.get(column) for column in key_columns)
                    if updated_key != key:
                        conflicting = index.get(updated_key, ())
                        if any(candidate != slot for candidate in conflicting):
                            raise SchemaError(f"duplicate primary key {updated_key!r}")
                    record = self._encode_record(updated, order)
                    row_count = header["row_count"]
                    key_unchanged = all(updated.get(column) == expected for column, expected in zip(key_columns, key))
            self._commit_locked(
                [(slot, record)],
                row_count,
                preserve_key_cache=bool(positions) and key_unchanged,
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
                raise SchemaError(f"table {self.definition.eng_name!r} reached max_elements {self.max_records}")
            prepared = [dict(row) for row in rows]
            for row in prepared:
                self._validate_row(row)
            with self._mapped() as (_handle, mapping):
                index = self._key_index_locked(mapping, header["generation"], key_columns)
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
                additions: list[tuple[tuple[Any, ...], int]] = []
                for position, (slot, row) in enumerate(zip(free_slots, prepared)):
                    key = tuple(row[column] for column in key_columns)
                    if key in used:
                        raise SchemaError(f"duplicate primary key {key!r}")
                    used.add(key)
                    entries.append((slot, self._encode_record(row, order + position)))
                    additions.append((key, slot))
            self._commit_locked(
                entries,
                header["row_count"] + len(entries),
                preserve_key_cache=True,
                key_cache_additions=(key_columns, additions),
            )
            return len(entries)

    def set(self, key_columns: Sequence[str], key: tuple[Any, ...], row: Mapping[str, Any], *, upsert: bool = False) -> int:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            with self._mapped() as (_handle, mapping):
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
                conflicting = index.get(replacement_key, ())
                if any(candidate != slot for candidate in conflicting):
                    raise SchemaError(f"duplicate primary key {replacement_key!r}")
            record = self._encode_record(row, order)
            key_unchanged = replacement_key == key
            self._commit_locked(
                [(slot, record)],
                row_count,
                preserve_key_cache=bool(positions) and key_unchanged,
            )
            return 1

    def delete(self, key_columns: Sequence[str], key: tuple[Any, ...]) -> int:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            with self._mapped() as (_handle, mapping):
                index = self._key_index_locked(mapping, header["generation"], key_columns)
                slot = self._find_slot(key_columns, key, index)
            self._commit_locked([(slot, bytes(self.record_size))], header["row_count"] - 1)
            return 1

    def update_many(self, key_columns: Sequence[str], updates: Sequence[tuple[tuple[Any, ...], Mapping[str, Any]]]) -> int:
        with _locked(self.lock_path, timeout=self.lock_timeout):
            self._recover_locked()
            header = self._read_header()
            entries: list[tuple[int, bytes]] = []
            seen: set[tuple[Any, ...]] = set()
            with self._mapped() as (_handle, mapping):
                index = self._key_index_locked(mapping, header["generation"], key_columns)
                for key, patch in updates:
                    if key in seen:
                        raise ValueError(f"duplicate key in update_many: {key!r}")
                    seen.add(key)
                    slot = self._find_slot(key_columns, key, index)
                    order, current = self._read_record(mapping, slot)
                    assert current is not None
                    updated = dict(current)
                    updated.update(copy.deepcopy(dict(patch)))
                    self._validate_row(updated)
                    entries.append((slot, self._encode_record(updated, order)))
            self._commit_locked(entries, header["row_count"], preserve_key_cache=True)
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
            self._commit_locked([(slot, bytes(self.record_size)) for slot in slots], header["row_count"] - len(slots))
            return len(slots)

    def replace_snapshot_locked(self, rows: Sequence[Mapping[str, Any]], generation: int) -> None:
        """Rewrite a fixed table through the binary WAL, in place.

        Fixed tables do not need an atomic file replacement: the WAL is made
        durable before any slot is changed, and the generation header is
        published last.  Keeping the file identity stable also lets readers
        reuse a memory mapping and avoids Windows ``os.replace`` contention.
        """

        if len(rows) > self.max_records:
            raise SchemaError(f"table {self.definition.eng_name!r} reached max_elements {self.max_records}")
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
        if definition is None and self.path.exists():
            try:
                with self.path.open("rb") as handle:
                    fixed_magic = handle.read(len(_FIXED_MAGIC))
            except PermissionError:
                # A legacy table may be atomically replaced by another
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
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.wal_path = self.path.with_suffix(self.path.suffix + ".wal")
        self._fixed_store = (
            _FixedRecordStore(self.path, definition, self.lock_path, self.lock_timeout)
            if definition is not None
            else None
        )
        self._cache: Optional[_Snapshot] = None
        self._cache_guard = threading.RLock()
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
            lookup = key if isinstance(key, tuple) else (key,)
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

        return TableKeyValue(self, key_column=key_column)

    @property
    def kv(self) -> "TableKeyValue":
        """Return the default key-value view for this table."""

        return self.key_value()

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
        elif isinstance(key, tuple) and len(key) == len(self.definition.primary_keys):
            values = key
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
        if (
            self._fixed_store is not None
            and self._fixed_store.active
            and self.definition is not None
            and isinstance(where, Mapping)
            and tuple(str(field) for field in where) == self.definition.primary_keys
        ):
            if limit == 0 or offset > 0:
                return []
            key = tuple(where[column] for column in self.definition.primary_keys)
            try:
                row = self._fixed_store.get(self.definition.primary_keys, key)
            except KeyError:
                return []
            if not predicate(row):
                return []
            if columns is not None:
                column_items = [columns] if isinstance(columns, str) else columns
                selected = [_unquote_identifier(str(column)) for column in column_items]
                row = {column: row.get(column) for column in selected}
            return [copy.deepcopy(row)]
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

        if isinstance(where, Mapping) and where:
            try:
                where_values = copy.deepcopy(dict(where))
                json.dumps(where_values, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"where contains a non-JSON value: {exc}") from exc

            if self._fixed_store is not None and self._fixed_store.active and self.definition is not None:
                key_columns = self.definition.primary_keys
                if tuple(str(field) for field in where_values) == key_columns:
                    if limit == 0:
                        return 0
                    key_values = tuple(where_values[column] for column in key_columns)
                    try:
                        return self._fixed_store.update(key_columns, key_values, patch)
                    except KeyError:
                        return 0

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

    def delete(
        self,
        where: Optional[Union[str, Mapping[str, Any], Callable[[Mapping[str, Any]], bool]]] = None,
        *,
        limit: Optional[int] = None,
    ) -> int:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        predicate = _compile_where(where)

        if isinstance(where, Mapping) and where:
            try:
                where_values = copy.deepcopy(dict(where))
                json.dumps(where_values, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"where contains a non-JSON value: {exc}") from exc

            if self._fixed_store is not None and self._fixed_store.active and self.definition is not None:
                key_columns = self.definition.primary_keys
                if tuple(str(field) for field in where_values) == key_columns:
                    if limit == 0:
                        return 0
                    key_values = tuple(where_values[column] for column in key_columns)
                    try:
                        return self._fixed_store.delete(key_columns, key_values)
                    except KeyError:
                        return 0

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

    def sort(self, *, key: Optional[str] = None, reverse: bool = False) -> None:
        """Sort the physical row order, like ``list.sort``."""

        order = None if key is None else [(key, "DESC" if reverse else "ASC")]

        def reorder(rows: list[dict[str, Any]], columns: list[str]) -> None:
            _sort_rows(rows, order)

        self._mutate(reorder, copy_rows=False)

    # ---- list-like API ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self._read_snapshot().rows)

    def _close(self) -> None:
        if self._fixed_store is not None:
            self._fixed_store.close()
        with self._cache_guard:
            self._cache = None

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
        elif isinstance(key, tuple) and len(key) == len(self.key_columns):
            values = key
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

    def require(self, key: Any) -> dict[str, Any]:
        """Return one detached row, raising ``KeyError`` when it is absent."""

        if self.table._fixed_store is not None and self.table._fixed_store.active:
            return self.table._fixed_store.get(self.key_columns, self._normalise_key(key, for_write=False))
        snapshot = self.table._read_snapshot()
        return copy.deepcopy(snapshot.rows[self._position(snapshot, key)])

    def get(self, key: Any, default: Any = None) -> Any:
        """Return one detached row or ``default`` when the key is absent."""

        try:
            return self.require(key)
        except KeyError:
            return default

    def contains(self, key: Any) -> bool:
        try:
            self.require(key)
        except KeyError:
            return False
        return True

    __contains__ = contains

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
        return len(self.keys())

    def __repr__(self) -> str:
        return f"TableKeyValue(table={self.table.name!r}, key_columns={self.key_columns!r})"


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


Database = FileMappedDatabase
FileMappedDB = FileMappedDatabase
MappedDatabase = FileMappedDatabase

