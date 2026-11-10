"""Embedded RTDB schema metadata stored inside SQLite files.

The SQLite backend historically used JSON files as its schema registry.  The
embedded registry makes the database file self describing while keeping JSON
and Python definitions available as import/export formats.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from contextlib import closing

from .table_schema import DatabaseDefinition, SchemaError, TableDefinition

EMBEDDED_DATABASE_TABLE = 'rtdb_schema_database'
EMBEDDED_TABLES_TABLE = 'rtdb_schema_tables'
EMBEDDED_COLUMNS_TABLE = 'rtdb_schema_columns'
EMBEDDED_TABLES = (EMBEDDED_DATABASE_TABLE, EMBEDDED_TABLES_TABLE, EMBEDDED_COLUMNS_TABLE)


def _quote(value):
    return '"' + str(value).replace('"', '""') + '"'


def definition_fingerprint(definition: TableDefinition) -> str:
    return hashlib.sha256(json.dumps(
        definition.to_mapping(), sort_keys=True, ensure_ascii=False,
    ).encode('utf-8')).hexdigest()


def ensure_embedded_schema(connection: sqlite3.Connection, database_name: str, database_label: str | None = None):
    """Create the internal schema tables and update the database label."""

    connection.execute(
        f'CREATE TABLE IF NOT EXISTS {_quote(EMBEDDED_DATABASE_TABLE)} ('
        'version INTEGER PRIMARY KEY CHECK(version=1), '
        'eng_name TEXT NOT NULL, chn_name TEXT NOT NULL)'
    )
    connection.execute(
        f'CREATE TABLE IF NOT EXISTS {_quote(EMBEDDED_TABLES_TABLE)} ('
        'table_name TEXT PRIMARY KEY, chn_name TEXT NOT NULL, '
        'ordinal INTEGER NOT NULL, fingerprint TEXT NOT NULL)'
    )
    connection.execute(
        f'CREATE TABLE IF NOT EXISTS {_quote(EMBEDDED_COLUMNS_TABLE)} ('
        'table_name TEXT NOT NULL, ordinal INTEGER NOT NULL, '
        'eng_name TEXT NOT NULL, chn_name TEXT NOT NULL, data_type TEXT NOT NULL, '
        'primary_key INTEGER NOT NULL CHECK(primary_key IN (0,1)), '
        'disp_type TEXT NOT NULL, disp_menu TEXT NOT NULL, '
        'has_default INTEGER NOT NULL CHECK(has_default IN (0,1)), '
        'default_json TEXT, PRIMARY KEY(table_name, ordinal), '
        f'UNIQUE(table_name, eng_name), FOREIGN KEY(table_name) '
        f'REFERENCES {_quote(EMBEDDED_TABLES_TABLE)}(table_name) ON DELETE CASCADE)'
    )
    connection.execute(
        f'INSERT INTO {_quote(EMBEDDED_DATABASE_TABLE)}(version, eng_name, chn_name) VALUES (1, ?, ?) '
        f'ON CONFLICT(version) DO UPDATE SET eng_name=excluded.eng_name, chn_name=excluded.chn_name',
        (database_name, database_label or database_name),
    )


def write_embedded_definition(connection: sqlite3.Connection, definition: TableDefinition,
                              database_name: str, database_label: str | None = None,
                              ordinal: int | None = None):
    """Insert or replace one table definition in the embedded registry."""

    ensure_embedded_schema(connection, database_name, database_label)
    table_name = definition.eng_name
    if ordinal is None:
        found = connection.execute(
            f'SELECT ordinal FROM {_quote(EMBEDDED_TABLES_TABLE)} WHERE table_name=?',
            (table_name,),
        ).fetchone()
        if found is not None:
            ordinal = int(found[0])
        else:
            ordinal = connection.execute(
                f'SELECT COALESCE(MAX(ordinal), -1) + 1 FROM {_quote(EMBEDDED_TABLES_TABLE)}'
            ).fetchone()[0]
    connection.execute(
        f'INSERT INTO {_quote(EMBEDDED_TABLES_TABLE)}(table_name, chn_name, ordinal, fingerprint) '
        'VALUES (?, ?, ?, ?) ON CONFLICT(table_name) DO UPDATE SET '
        'chn_name=excluded.chn_name, ordinal=excluded.ordinal, fingerprint=excluded.fingerprint',
        (table_name, definition.chn_name, int(ordinal), definition_fingerprint(definition)),
    )
    connection.execute(f'DELETE FROM {_quote(EMBEDDED_COLUMNS_TABLE)} WHERE table_name=?', (table_name,))
    values = []
    for ordinal, column in enumerate(definition.columns):
        has_default = column.default_value is not None
        values.append((
            table_name, ordinal, column.eng_name, column.chn_name, column.data_type,
            int(column.primary_key), column.disp_type, column.disp_menu,
            int(has_default), json.dumps(column.default_value, ensure_ascii=False, allow_nan=False)
            if has_default else None,
        ))
    connection.executemany(
        f'INSERT INTO {_quote(EMBEDDED_COLUMNS_TABLE)} '
        '(table_name, ordinal, eng_name, chn_name, data_type, primary_key, '
        'disp_type, disp_menu, has_default, default_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        values,
    )


def remove_embedded_definition(connection: sqlite3.Connection, table_name: str):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (EMBEDDED_TABLES_TABLE,)
    ).fetchone():
        return
    connection.execute(f'DELETE FROM {_quote(EMBEDDED_COLUMNS_TABLE)} WHERE table_name=?', (table_name,))
    connection.execute(f'DELETE FROM {_quote(EMBEDDED_TABLES_TABLE)} WHERE table_name=?', (table_name,))


def _read_embedded_database(connection: sqlite3.Connection, path: Path) -> DatabaseDefinition | None:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (EMBEDDED_TABLES_TABLE,)
    ).fetchone():
        return None
    table_rows = connection.execute(
        f'SELECT table_name, chn_name, ordinal, fingerprint FROM {_quote(EMBEDDED_TABLES_TABLE)} '
        'ORDER BY ordinal, table_name'
    ).fetchall()
    if not table_rows:
        return None
    database_row = connection.execute(
        f'SELECT eng_name, chn_name FROM {_quote(EMBEDDED_DATABASE_TABLE)} WHERE version=1'
    ).fetchone()
    database_name, database_label = database_row if database_row else (path.stem, path.stem)
    tables = []
    for table_name, chn_name, _ordinal, stored_fingerprint in table_rows:
        column_rows = connection.execute(
            f'SELECT eng_name, chn_name, data_type, primary_key, disp_type, disp_menu, '
            f'has_default, default_json FROM {_quote(EMBEDDED_COLUMNS_TABLE)} '
            'WHERE table_name=? ORDER BY ordinal', (table_name,)
        ).fetchall()
        if not column_rows:
            raise SchemaError(f'embedded schema table {table_name!r} has no columns')
        columns = []
        for eng_name, column_label, data_type, primary_key, disp_type, disp_menu, has_default, default_json in column_rows:
            if bool(has_default):
                if default_json is None:
                    raise SchemaError(f'embedded schema column {table_name}.{eng_name} has an invalid default')
                try:
                    default_value = json.loads(default_json)
                except (TypeError, json.JSONDecodeError) as error:
                    raise SchemaError(f'embedded schema column {table_name}.{eng_name} has an invalid default') from error
            else:
                default_value = None
            columns.append({
                'eng_name': eng_name, 'chn_name': column_label, 'data_type': data_type,
                'primary_key': bool(primary_key), 'disp_type': disp_type, 'disp_menu': disp_menu,
                **({'default_value': default_value} if bool(has_default) else {}),
            })
        mapping = {'head': {'eng_name': table_name, 'chn_name': chn_name}, 'columns': columns}
        parsed = TableDefinition.from_mapping(mapping, path.parent, table_name)
        if stored_fingerprint and stored_fingerprint != definition_fingerprint(parsed):
            raise SchemaError(f'embedded schema fingerprint mismatch for {table_name!r}')
        tables.append(replace(parsed, path=path))
    return DatabaseDefinition(
        eng_name=str(database_name or path.stem), chn_name=str(database_label or database_name or path.stem),
        tables=tuple(tables), path=path,
    )


def load_embedded_database(path: str | Path) -> DatabaseDefinition | None:
    """Load embedded definitions from one SQLite file, if present."""

    path = Path(path).expanduser().resolve()
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(str(path))) as connection:
            return _read_embedded_database(connection, path)
    except sqlite3.Error as error:
        raise SchemaError(f'cannot read embedded schema {path}: {error}') from error


def find_embedded_table(directory: str | Path, table_name: str):
    """Return ``(definition, database_definition)`` for a logical table."""

    root = Path(directory).expanduser().resolve()
    for path in sorted(root.glob('*.sqlite3'), key=lambda candidate: candidate.name.lower()):
        document = load_embedded_database(path)
        if document is None:
            continue
        for definition in document.tables:
            if definition.eng_name == table_name:
                return definition, document
    return None
