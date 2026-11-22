"""Managed secondary indexes; primary keys are always preserved."""

_UNINDEXABLE_AUTO_TYPES = frozenset({'blob', 'bytes', 'binary', 'json', 'list', 'dict', 'array', 'object'})


def quote(value):
    return '"'+value.replace('"','""')+'"'


def reconcile_indexes(connection, data_name, definition):
    prefix=(data_name+'_' if data_name!='data' else '')+'idx_'
    fields={name:f'c{i}' for i,name in enumerate(definition.column_names)}
    col_types={col.eng_name: str(col.data_type or '').lower() for col in definition.columns}
    auto_mode = getattr(definition, 'indexes', None) is None
    desired={}
    for columns in definition.secondary_indexes:
        if columns==definition.primary_keys:
            continue
        if auto_mode and any(col_types.get(name) in _UNINDEXABLE_AUTO_TYPES for name in columns):
            continue
        desired[prefix+'_'.join(fields[name] for name in columns)]=tuple(fields[name] for name in columns)
    existing={row[1]:tuple(item[2] for item in connection.execute(f'PRAGMA index_info({quote(row[1])})'))
              for row in connection.execute(f'PRAGMA index_list({quote(data_name)})').fetchall() if row[1].startswith(prefix)}
    for name,columns in existing.items():
        if desired.get(name)!=columns:connection.execute(f'DROP INDEX {quote(name)}')
    for name,columns in desired.items():
        if existing.get(name)!=columns:
            connection.execute(f'CREATE INDEX {quote(name)} ON {quote(data_name)} ('+','.join(quote(c) for c in columns)+')')
