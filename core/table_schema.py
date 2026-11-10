"""JSON table-definition files for :mod:`core.file_mapped_db`.

The definition directory accepts either one UTF-8 JSON file per table or one
grouped UTF-8 JSON file containing multiple tables:

``<table>.json``
    One JSON object containing a ``head`` object and a ``columns`` array.
``<database>.json``
    A JSON object containing optional ``database`` metadata and a ``tables``
    array. Each item in ``tables`` uses the same ``head``/``columns`` shape.

The JSON field names are fixed English names: ``chn_name``, ``eng_name``,
``primary_key``, ``data_type``, ``disp_type``, ``disp_menu`` and optional ``default_value``. Definitions are data only; no executable code
is evaluated.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Union

__all__ = [
    "ColumnDefinition",
    "DatabaseDefinition",
    "SchemaError",
    "SchemaRegistry",
    "TableDefinition",
    "TableHead",
    "load_table_definition",
]


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE_FILE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SchemaError(ValueError):
    """Raised when a table definition is missing, malformed or inconsistent."""


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _parse_payload_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = _clean(value).lower()
    if text in {"true", "yes", "y", "是", "真", "on", "1"}:
        return True
    if text in {"false", "no", "n", "否", "假", "off", "0"}:
        return False
    raise SchemaError(f"{field} must be boolean")


@dataclass(frozen=True)
class TableHead:
    """Whole-table metadata from the JSON ``head`` object."""

    chn_name: str
    eng_name: str
    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.eng_name):
            raise SchemaError(f"eng_name must be an English identifier, got {self.eng_name!r}")
        if not self.chn_name.strip():
            raise SchemaError("chn_name is required")

@dataclass(frozen=True)
class ColumnDefinition:
    """One column's metadata from the JSON ``columns`` array."""

    chn_name: str
    eng_name: str
    primary_key: bool
    data_type: str
    disp_type: str
    disp_menu: str
    default_value: Any = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.eng_name):
            raise SchemaError(f"eng_name must be an English identifier, got {self.eng_name!r}")
        if not self.chn_name.strip():
            raise SchemaError("chn_name is required")
        if not self.data_type.strip():
            raise SchemaError(f"data_type is required for {self.eng_name!r}")
        if not self.disp_type.strip():
            raise SchemaError(f"disp_type is required for {self.eng_name!r}")
        if self.default_value is not None:
            value = copy.deepcopy(self.default_value)
            kind = self.data_type.strip().lower()
            try:
                # The browser transports integers outside its exact range as strings.
                if kind in {"int", "integer", "long"}:
                    if isinstance(value, str) and re.fullmatch(r'-?\d+', value):
                        value = int(value)
                    if isinstance(value, int) and not -(2**63) <= value < 2**63:
                        raise ValueError('integer outside signed 64-bit range')
                TableDefinition._validate_value(value, self, 1)
                json.dumps(value, allow_nan=False)
                if self.primary_key:
                    hash(value)
                    if isinstance(value, str) and not value.strip():
                        raise ValueError('primary key cannot be blank')
            except (TypeError, ValueError) as error:
                raise SchemaError(f"column {self.eng_name!r} default_value: {error}") from error
            object.__setattr__(self, 'default_value', value)

    @property
    def is_primary_key(self) -> bool:
        return self.primary_key

    @property
    def disp_menu_items(self) -> tuple[str, ...]:
        """Return menu entries split on ``|`` while preserving the raw value."""

        if not self.disp_menu.strip():
            return ()
        return tuple(item.strip() for item in self.disp_menu.split("|") if item.strip())


@dataclass(frozen=True)
class TableDefinition:
    """Validated metadata from one ``<table>.json`` definition file."""

    head: TableHead
    columns: tuple[ColumnDefinition, ...]
    path: Path
    _column_names: tuple[str, ...] = field(init=False, repr=False, compare=False)
    _primary_keys: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.columns:
            raise SchemaError(f"table {self.eng_name!r} must have at least one column")
        names = [column.eng_name for column in self.columns]
        if len(names) != len(set(names)):
            raise SchemaError(f"table {self.eng_name!r} contains duplicate column names")
        if not any(column.primary_key for column in self.columns):
            raise SchemaError(f"table {self.eng_name!r} must have at least one primary key")
        # Definitions are immutable after validation.  Cache these hot-path
        # projections once instead of allocating a tuple on every CRUD call.
        object.__setattr__(self, "_column_names", tuple(names))
        object.__setattr__(
            self,
            "_primary_keys",
            tuple(column.eng_name for column in self.columns if column.primary_key),
        )

    @property
    def chn_name(self) -> str:
        return self.head.chn_name

    @property
    def eng_name(self) -> str:
        return self.head.eng_name

    @property
    def column_names(self) -> tuple[str, ...]:
        return self._column_names

    @property
    def primary_keys(self) -> tuple[str, ...]:
        return self._primary_keys

    def column(self, name: str) -> ColumnDefinition:
        for column in self.columns:
            if column.eng_name == name:
                return column
        raise KeyError(name)

    @classmethod
    def load(cls, directory: Union[str, os.PathLike[str]], table_name: str) -> "TableDefinition":
        table_name = _validate_table_name(table_name)
        root = Path(directory).expanduser().resolve()
        return cls.from_json_file(root / f"{table_name}.json")

    @classmethod
    def from_json_file(cls, path: Union[str, os.PathLike[str]]) -> "TableDefinition":
        path = Path(path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise SchemaError(f"definition file does not exist: {path}")
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SchemaError(f"invalid JSON definition {path}: {exc}") from exc
        definition = cls.from_mapping(payload, path.parent, path.stem)
        return cls(head=definition.head, columns=definition.columns, path=path)

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        directory: Union[str, os.PathLike[str]],
        table_name: Optional[str] = None,
    ) -> "TableDefinition":
        """Build a validated definition from the JSON shape used by the web API."""

        if not isinstance(payload, Mapping):
            raise SchemaError("definition payload must be a JSON object")
        head_value = payload.get("head")
        columns_value = payload.get("columns")
        if not isinstance(head_value, Mapping):
            raise SchemaError("definition payload requires a head object")
        if not isinstance(columns_value, list):
            raise SchemaError("definition payload requires a columns array")
        if "max_elements" in head_value:
            raise SchemaError("head.max_elements has been removed from the table definition")
        eng_name = _clean(head_value.get("eng_name"))
        if table_name is not None and eng_name != _validate_table_name(table_name):
            raise SchemaError(f"head.eng_name {eng_name!r} does not match table name {table_name!r}")
        head = TableHead(
            chn_name=_clean(head_value.get("chn_name")),
            eng_name=eng_name,
        )
        columns: list[ColumnDefinition] = []
        for index, value in enumerate(columns_value, start=1):
            if not isinstance(value, Mapping):
                raise SchemaError(f"columns[{index - 1}] must be an object")
            if "data_length" in value:
                raise SchemaError(f"columns[{index - 1}].data_length has been removed from the table definition")
            columns.append(
                ColumnDefinition(
                    chn_name=_clean(value.get("chn_name")),
                    eng_name=_clean(value.get("eng_name")),
                    primary_key=_parse_payload_bool(value.get("primary_key"), f"columns[{index - 1}].primary_key"),
                    data_type=_clean(value.get("data_type")),
                    disp_type=_clean(value.get("disp_type")),
                    disp_menu=_clean(value.get("disp_menu", "")),
                    default_value=value.get("default_value"),
                )
            )
        root = Path(directory).expanduser().resolve()
        return cls(
            head=head,
            columns=tuple(columns),
            path=root / f"{head.eng_name}.json",
        )

    def apply_defaults(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Fill omitted fields only; explicit NULL and supplied values are preserved."""
        result = dict(row)
        for column in self.columns:
            if column.eng_name not in result and column.default_value is not None:
                result[column.eng_name] = copy.deepcopy(column.default_value)
        return result

    def validate_rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Validate row shape and primary-key presence/uniqueness."""

        values = list(rows)
        allowed = set(self.column_names)
        key_values: set[tuple[Any, ...]] = set()
        for row_number, row in enumerate(values, start=1):
            unknown = set(row) - allowed
            if unknown:
                raise SchemaError(f"row {row_number}: unknown columns {sorted(unknown)!r}")
            for column in self.columns:
                if column.primary_key:
                    if column.eng_name not in row or row[column.eng_name] is None:
                        raise SchemaError(f"row {row_number}: primary key {column.eng_name!r} is required")
                if column.eng_name not in row or row[column.eng_name] is None:
                    continue
                self._validate_value(row[column.eng_name], column, row_number)
            key = tuple(row.get(name) for name in self.primary_keys)
            try:
                duplicate = key in key_values
                key_values.add(key)
            except TypeError as exc:
                raise SchemaError(f"row {row_number}: primary-key values must be hashable") from exc
            if duplicate:
                raise SchemaError(f"row {row_number}: duplicate primary key {key!r}")

    @staticmethod
    def _validate_value(value: Any, column: ColumnDefinition, row_number: int) -> None:
        kind = column.data_type.strip().lower()
        valid = True
        if kind in {"int", "integer", "long"}:
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif kind in {"float", "double", "number", "decimal"}:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif kind in {"bool", "boolean"}:
            valid = isinstance(value, bool)
        elif kind in {"str", "string", "text", "varchar", "char", "date", "datetime", "time"}:
            valid = isinstance(value, str)
        elif kind in {"list", "array"}:
            valid = isinstance(value, list)
        elif kind in {"dict", "object", "json"}:
            valid = isinstance(value, (dict, list))
        if not valid:
            raise SchemaError(
                f"row {row_number}: column {column.eng_name!r} expects data_type {column.data_type!r}"
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "head": {
                "chn_name": self.chn_name,
                "eng_name": self.eng_name,
            },
            "columns": [
                {
                    "chn_name": column.chn_name,
                    "eng_name": column.eng_name,
                    "primary_key": column.primary_key,
                    "data_type": column.data_type,
                    "disp_type": column.disp_type,
                    "disp_menu": column.disp_menu,
                    **({"default_value": copy.deepcopy(column.default_value)} if column.default_value is not None else {}),
                }
                for column in self.columns
            ],
        }

    def to_json(self, directory: Union[str, os.PathLike[str]], *, overwrite: bool = False) -> Path:
        """Write one UTF-8 JSON definition file for this table."""

        root = Path(directory).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{self.eng_name}.json"
        if not overwrite and path.exists():
            raise FileExistsError(f"definition already exists for {self.eng_name!r}")
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.to_mapping(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        return path


@dataclass(frozen=True)
class DatabaseDefinition:
    """A JSON definition document containing one or more table definitions.

    The legacy document shape (one ``head`` and ``columns`` pair) remains
    valid.  New documents use ``tables`` and keep the database label optional::

        {"database": {"eng_name": "ems", "chn_name": "EMS"},
         "tables": [{"head": ..., "columns": ...}, ...]}

    ``path`` is the source JSON file shared by all tables in the document.
    """

    eng_name: str
    chn_name: str
    tables: tuple[TableDefinition, ...]
    path: Path

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.eng_name):
            raise SchemaError(f"database name must be an English identifier, got {self.eng_name!r}")
        if not self.tables:
            raise SchemaError(f"database {self.eng_name!r} must contain at least one table")
        names = [table.eng_name for table in self.tables]
        if len(names) != len(set(names)):
            raise SchemaError(f"database {self.eng_name!r} contains duplicate table names")

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(table.eng_name for table in self.tables)

    @property
    def is_grouped(self) -> bool:
        return len(self.tables) > 1 or self.path.stem != self.tables[0].eng_name

    def to_mapping(self) -> dict[str, Any]:
        return {
            "database": {"chn_name": self.chn_name, "eng_name": self.eng_name},
            "tables": [table.to_mapping() for table in self.tables],
        }


def load_database_definition(path: Union[str, os.PathLike[str]]) -> DatabaseDefinition:
    """Load either a legacy single-table JSON or a grouped JSON document."""

    path = Path(path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise SchemaError(f"definition file does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"invalid JSON definition {path}: {exc}") from exc
    if isinstance(payload, Mapping) and isinstance(payload.get("tables"), list):
        values = payload["tables"]
        database = payload.get("database") if isinstance(payload.get("database"), Mapping) else {}
        eng_name = _clean(database.get("eng_name")) or path.stem
        chn_name = _clean(database.get("chn_name")) or eng_name
    elif isinstance(payload, list):
        values = payload
        eng_name, chn_name = path.stem, path.stem
    else:
        values = [payload]
        head = payload.get("head") if isinstance(payload, Mapping) else {}
        if not isinstance(head, Mapping):
            head = {}
        eng_name = _clean(head.get("eng_name")) or path.stem
        chn_name = _clean(head.get("chn_name")) or eng_name
    if not isinstance(values, list) or not values:
        raise SchemaError("definition document requires a non-empty tables array")
    tables: list[TableDefinition] = []
    for value in values:
        table = TableDefinition.from_mapping(value, path.parent)
        tables.append(TableDefinition(head=table.head, columns=table.columns, path=path))
    return DatabaseDefinition(eng_name=eng_name, chn_name=chn_name, tables=tuple(tables), path=path)


def _validate_table_name(name: str) -> str:
    name = _clean(name)
    if not _TABLE_FILE_NAME.fullmatch(name):
        raise SchemaError(f"table name must be an English identifier, got {name!r}")
    return name


def load_table_definition(directory: Union[str, os.PathLike[str]], table_name: str) -> TableDefinition:
    """Load one table's ``<table>.json`` definition file."""

    return TableDefinition.load(directory, table_name)


class SchemaRegistry:
    """Discover and cache table definitions in a directory."""

    def __init__(self, directory: Union[str, os.PathLike[str]]) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self._cache: dict[str, TableDefinition] = {}
        self._database_cache: dict[Path, DatabaseDefinition] = {}
        self._source_by_table: dict[str, Path] = {}

    def _definition_files(self) -> Iterator[Path]:
        """Yield user-authored definition documents only.

        The web service keeps crash-recovery journals and lock/temporary JSON
        documents beside the definitions.  They are implementation details and
        must never be interpreted as table definitions during discovery.
        """
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.json")):
            if path.is_file() and not path.name.startswith(".") and not path.name.endswith(".tmp"):
                yield path

    def _read_database(self, path: Path) -> DatabaseDefinition:
        path = path.resolve()
        document = self._database_cache.get(path)
        if document is None:
            document = load_database_definition(path)
            self._database_cache[path] = document
            for table in document.tables:
                previous = self._source_by_table.get(table.eng_name)
                if previous is not None and previous != path:
                    raise SchemaError(f"table {table.eng_name!r} is defined by both {previous.name} and {path.name}")
                self._source_by_table[table.eng_name] = path
                self._cache[table.eng_name] = table
        return document

    def load(self, table_name: str) -> TableDefinition:
        table_name = _validate_table_name(table_name)
        definition = self._cache.get(table_name)
        if definition is None:
            for path in self._definition_files():
                self._read_database(path)
                if table_name in self._cache:
                    break
            definition = self._cache.get(table_name)
            if definition is None:
                raise SchemaError(f"definition file does not exist for table: {table_name}")
        return definition

    def names(self) -> tuple[str, ...]:
        if not self.directory.exists():
            return ()
        for path in self._definition_files():
            self._read_database(path)
        return tuple(sorted(self._cache))

    def source_path(self, table_name: str) -> Path:
        self.load(table_name)
        return self._source_by_table[table_name]

    def database_name(self, table_name: str) -> str:
        return self.source_path(table_name).stem

    def database_definition(self, table_name: str) -> DatabaseDefinition:
        return self._read_database(self.source_path(table_name))

    def database_definitions(self) -> tuple[DatabaseDefinition, ...]:
        if not self.directory.exists():
            return ()
        return tuple(self._read_database(path) for path in self._definition_files())

    def clear(self) -> None:
        self._cache.clear()
        self._database_cache.clear()
        self._source_by_table.clear()

    def definitions(self) -> tuple[TableDefinition, ...]:
        return tuple(self.load(name) for name in self.names())

    def __iter__(self) -> Iterator[TableDefinition]:
        return iter(self.definitions())

    def __getitem__(self, table_name: str) -> TableDefinition:
        return self.load(table_name)

