"""Embedded RTDB schema metadata stored inside SQLite files.

The SQLite backend historically used JSON files as its schema registry.  The
embedded registry makes the database file self describing while keeping JSON
and Python definitions available as import/export formats.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from contextlib import closing

from .table_schema import DatabaseDefinition, NATIVE_IDENTIFIER, SchemaError, TableDefinition

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
    _ensure_indexes_column(connection)
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
    _store_embedded_definition(connection, definition, ordinal)


def write_native_definition(connection: sqlite3.Connection, definition: TableDefinition,
                            ordinal: int | None = None) -> None:
    """Replace one native table definition without changing database identity."""
    if definition.native_table is None or embedded_storage_format(connection) != 'native':
        raise SchemaError('native SQLite definition required')
    _ensure_indexes_column(connection)
    _store_embedded_definition(connection, definition, ordinal)


def _ensure_indexes_column(connection: sqlite3.Connection) -> None:
    if 'indexes_json' not in {row[1] for row in connection.execute(f'PRAGMA table_info({_quote(EMBEDDED_TABLES_TABLE)})')}:
        connection.execute(f'ALTER TABLE {_quote(EMBEDDED_TABLES_TABLE)} ADD COLUMN indexes_json TEXT')


def _store_embedded_definition(connection: sqlite3.Connection, definition: TableDefinition,
                               ordinal: int | None) -> None:
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
    connection.execute(f'UPDATE {_quote(EMBEDDED_TABLES_TABLE)} SET indexes_json=? WHERE table_name=?',
                       (json.dumps(definition.to_mapping()['indexes']) if definition.indexes is not None else None,table_name))
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
    index_column='indexes_json' if 'indexes_json' in {row[1] for row in connection.execute(f'PRAGMA table_info({_quote(EMBEDDED_TABLES_TABLE)})')} else 'NULL'
    table_rows = connection.execute(
        f'SELECT table_name, chn_name, ordinal, fingerprint, {index_column} FROM {_quote(EMBEDDED_TABLES_TABLE)} '
        'ORDER BY ordinal, table_name'
    ).fetchall()
    storage_format = embedded_storage_format(connection)
    database_row = connection.execute(
        f'SELECT eng_name, chn_name FROM {_quote(EMBEDDED_DATABASE_TABLE)} WHERE version=1'
    ).fetchone()
    database_name, database_label = database_row if database_row else (path.stem, path.stem)
    tables = []
    for table_name, chn_name, _ordinal, stored_fingerprint, indexes_json in table_rows:
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
        if indexes_json is not None:
            try:mapping['indexes']=json.loads(indexes_json)
            except (TypeError,ValueError) as error:raise SchemaError(f'invalid embedded indexes for {table_name}') from error
        native_table = table_name if storage_format == 'native' else None
        parsed = TableDefinition.from_mapping(mapping, path.parent, table_name, native_table=native_table)
        if (stored_fingerprint or native_table is not None) and stored_fingerprint != definition_fingerprint(parsed):
            raise SchemaError(f'embedded schema fingerprint mismatch for {table_name!r}')
        tables.append(replace(parsed, path=path))
    return DatabaseDefinition(
        eng_name=str(database_name or path.stem), chn_name=str(database_label or database_name or path.stem),
        tables=tuple(tables), path=path,
        grouped=storage_format == 'native' or not tables or bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'rtdb_data_%'").fetchone()),
        storage_format=storage_format,
    )


def embedded_storage_format(connection: sqlite3.Connection) -> str:
    """Only opt in to native storage via the embedded database marker."""
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info({_quote(EMBEDDED_DATABASE_TABLE)})')}
    if 'storage_format' not in columns:
        return 'rtdb'
    row = connection.execute(
        f'SELECT storage_format FROM {_quote(EMBEDDED_DATABASE_TABLE)} WHERE version=1'
    ).fetchone()
    if row is None or row[0] not in ('rtdb', 'native'):
        raise SchemaError('invalid SQLite embedded storage_format')
    return row[0]


def validate_native_table(connection: sqlite3.Connection, definition: TableDefinition) -> None:
    """Check that the embedded layout references an actual keyed SQLite table."""
    name = definition.eng_name
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (name,),
    ).fetchone()
    if not exists:
        raise SchemaError(f'SQLite native table {name!r} is missing')
    columns = [row for row in connection.execute(f'PRAGMA table_xinfo({_quote(name)})') if row[6] != 1]
    if tuple(row[1] for row in columns) != definition.column_names:
        raise SchemaError(f'SQLite native table {name!r} columns differ from the embedded definition')
    primary_keys = {row[1] for row in columns if row[5]}
    keys = set(definition.primary_keys)
    if keys == primary_keys:
        return
    # Some application tables declare a non-null UNIQUE key separately.
    for _, index_name, unique, _, partial in connection.execute(f'PRAGMA index_list({_quote(name)})'):
        if not unique or partial:
            continue
        fields = {row[2] for row in connection.execute(f'PRAGMA index_info({_quote(index_name)})')}
        if fields == keys and all(row[3] for row in columns if row[1] in fields):
            return
    raise SchemaError(f'SQLite native table {name!r} does not have the declared primary key')


def native_database(path: str | Path) -> bool:
    """Inspect the marker alone, including when other metadata needs repair."""
    path = Path(path).resolve()
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
            return embedded_storage_format(connection) == 'native'
    except sqlite3.Error as error:
        # Legacy recovery paths can still inspect malformed SQLite files.
        if getattr(error, 'sqlite_errorcode', 0) in (sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT):
            return False
        raise


def sqlite_filename(filename: str) -> bool:
    """Accept a safe leaf name for discovering application-owned files."""
    return (
        isinstance(filename, str)
        and not re.search(r'[<>:"/\\|?*\x00-\x1f]', filename)
        and Path(filename).suffix.lower() in ('.db', '.sqlite', '.sqlite3')
        and filename[0] != '.'
    )


def legacy_sqlite_filename(filename: str) -> bool:
    return bool(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.sqlite3', filename))


def load_embedded_database(path: str | Path) -> DatabaseDefinition | None:
    """Load embedded definitions from one SQLite file, if present."""

    path = Path(path).expanduser().resolve()
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
            connection.execute('BEGIN')
            if not legacy_sqlite_filename(path.name) and embedded_storage_format(connection) != 'native':
                return None
            return _read_embedded_database(connection, path)
    except sqlite3.Error as error:
        if not legacy_sqlite_filename(path.name) and getattr(error, 'sqlite_errorcode', 0) in (
            sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT,
        ):
            return None
        raise SchemaError(f'cannot read embedded schema {path}: {error}') from error


def iter_embedded_databases(directory: str | Path, database_file=None):
    """Discover embedded RTDB databases, including marked application .db files."""
    root = Path(directory).expanduser().resolve()
    if database_file:
        filename = Path(database_file)
        paths = [root / filename] if filename.name == database_file else []
    else:
        paths = sorted(root.iterdir(), key=lambda path: path.name.lower()) if root.is_dir() else []
    for path in paths:
        if not sqlite_filename(path.name):
            continue
        document = load_embedded_database(path)
        if document is not None:
            yield document


def find_embedded_table(directory: str | Path, table_name: str, database_file=None):
    """Return the definition and database document for a logical table."""
    found = None
    for document in iter_embedded_databases(directory, database_file):
        for definition in document.tables:
            if definition.eng_name == table_name:
                if found is not None:
                    raise SchemaError(f'ambiguous table {table_name!r}; specify database_file')
                found = definition, document
    return found
