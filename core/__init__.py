"""Core data-access interfaces."""

from .file_mapped_db import (
    Database,
    FileMappedDatabase,
    FileMappedDB,
    MappedDatabase,
    MappedTable,
    QueryResult,
    RelationTable,
    StorageError,
    Table,
)
from .table_schema import (
    ColumnDefinition,
    SchemaError,
    SchemaRegistry,
    TableDefinition,
    TableHead,
    load_table_definition,
)

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
    "ColumnDefinition",
    "SchemaError",
    "SchemaRegistry",
    "TableDefinition",
    "TableHead",
    "load_table_definition",
]

