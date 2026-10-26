"""JSON table-definition files for :mod:`core.file_mapped_db`.

The definition directory contains one UTF-8 JSON file per table:

``<table>.json``
    One JSON object containing a ``head`` object and a ``columns`` array.

The JSON field names are fixed English names: ``chn_name``, ``eng_name``,
``max_elements``, ``primary_key``, ``data_type``, ``data_length``,
``disp_type`` and ``disp_menu``. Definitions are data only; no executable code
is evaluated.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Union

__all__ = [
    "ColumnDefinition",
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


def _parse_payload_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise SchemaError(f"{field} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{field} must be a non-negative integer") from exc
    if result < 0:
        raise SchemaError(f"{field} must be a non-negative integer")
    return result


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
    max_elements: int

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.eng_name):
            raise SchemaError(f"eng_name must be an English identifier, got {self.eng_name!r}")
        if not self.chn_name.strip():
            raise SchemaError("chn_name is required")
        if self.max_elements < 0:
            raise SchemaError("max_elements must be non-negative")

    @property
    def max_count(self) -> int:
        return self.max_elements


@dataclass(frozen=True)
class ColumnDefinition:
    """One column's metadata from the JSON ``columns`` array."""

    chn_name: str
    eng_name: str
    primary_key: bool
    data_type: str
    data_length: int
    disp_type: str
    disp_menu: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.eng_name):
            raise SchemaError(f"eng_name must be an English identifier, got {self.eng_name!r}")
        if not self.chn_name.strip():
            raise SchemaError("chn_name is required")
        if not self.data_type.strip():
            raise SchemaError(f"data_type is required for {self.eng_name!r}")
        if self.data_length < 0:
            raise SchemaError(f"data_length must be non-negative for {self.eng_name!r}")
        if not self.disp_type.strip():
            raise SchemaError(f"disp_type is required for {self.eng_name!r}")

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

    def __post_init__(self) -> None:
        if not self.columns:
            raise SchemaError(f"table {self.eng_name!r} must have at least one column")
        names = [column.eng_name for column in self.columns]
        if len(names) != len(set(names)):
            raise SchemaError(f"table {self.eng_name!r} contains duplicate column names")
        if not any(column.primary_key for column in self.columns):
            raise SchemaError(f"table {self.eng_name!r} must have at least one primary key")

    @property
    def chn_name(self) -> str:
        return self.head.chn_name

    @property
    def eng_name(self) -> str:
        return self.head.eng_name

    @property
    def max_elements(self) -> int:
        return self.head.max_elements

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.eng_name for column in self.columns)

    @property
    def primary_keys(self) -> tuple[str, ...]:
        return tuple(column.eng_name for column in self.columns if column.primary_key)

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
        eng_name = _clean(head_value.get("eng_name"))
        if table_name is not None and eng_name != _validate_table_name(table_name):
            raise SchemaError(f"head.eng_name {eng_name!r} does not match table name {table_name!r}")
        head = TableHead(
            chn_name=_clean(head_value.get("chn_name")),
            eng_name=eng_name,
            max_elements=_parse_payload_int(head_value.get("max_elements"), "head.max_elements"),
        )
        columns: list[ColumnDefinition] = []
        for index, value in enumerate(columns_value, start=1):
            if not isinstance(value, Mapping):
                raise SchemaError(f"columns[{index - 1}] must be an object")
            columns.append(
                ColumnDefinition(
                    chn_name=_clean(value.get("chn_name")),
                    eng_name=_clean(value.get("eng_name")),
                    primary_key=_parse_payload_bool(value.get("primary_key"), f"columns[{index - 1}].primary_key"),
                    data_type=_clean(value.get("data_type")),
                    data_length=_parse_payload_int(value.get("data_length"), f"columns[{index - 1}].data_length"),
                    disp_type=_clean(value.get("disp_type")),
                    disp_menu=_clean(value.get("disp_menu", "")),
                )
            )
        root = Path(directory).expanduser().resolve()
        return cls(
            head=head,
            columns=tuple(columns),
            path=root / f"{head.eng_name}.json",
        )

    def validate_rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Validate row shape, primary-key presence/uniqueness and max count."""

        values = list(rows)
        if len(values) > self.max_elements:
            raise SchemaError(
                f"table {self.eng_name!r} contains {len(values)} rows; maximum is {self.max_elements}"
            )
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
            if valid and column.data_length > 0 and len(value) > column.data_length:
                raise SchemaError(
                    f"row {row_number}: column {column.eng_name!r} exceeds data_length {column.data_length}"
                )
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
                "max_elements": self.max_elements,
            },
            "columns": [
                {
                    "chn_name": column.chn_name,
                    "eng_name": column.eng_name,
                    "primary_key": column.primary_key,
                    "data_type": column.data_type,
                    "data_length": column.data_length,
                    "disp_type": column.disp_type,
                    "disp_menu": column.disp_menu,
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

    def load(self, table_name: str) -> TableDefinition:
        table_name = _validate_table_name(table_name)
        definition = self._cache.get(table_name)
        if definition is None:
            definition = TableDefinition.load(self.directory, table_name)
            self._cache[table_name] = definition
        return definition

    def names(self) -> tuple[str, ...]:
        if not self.directory.exists():
            return ()
        return tuple(sorted(path.stem for path in self.directory.glob("*.json") if path.is_file()))

    def clear(self) -> None:
        self._cache.clear()

    def definitions(self) -> tuple[TableDefinition, ...]:
        return tuple(self.load(name) for name in self.names())

    def __iter__(self) -> Iterator[TableDefinition]:
        return iter(self.definitions())

    def __getitem__(self, table_name: str) -> TableDefinition:
        return self.load(table_name)

