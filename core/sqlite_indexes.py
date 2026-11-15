"""Managed secondary indexes; primary keys are always preserved."""
def quote(value):
    return '"'+value.replace('"','""')+'"'


def reconcile_indexes(connection, data_name, definition):
    prefix=(data_name+'_' if data_name!='data' else '')+'idx_'
    fields={name:f'c{i}' for i,name in enumerate(definition.column_names)}
    desired={prefix+'_'.join(fields[name] for name in columns):tuple(fields[name] for name in columns)
             for columns in definition.secondary_indexes if columns!=definition.primary_keys}
    existing={row[1]:tuple(item[2] for item in connection.execute(f'PRAGMA index_info({quote(row[1])})'))
              for row in connection.execute(f'PRAGMA index_list({quote(data_name)})').fetchall() if row[1].startswith(prefix)}
    for name,columns in existing.items():
        if desired.get(name)!=columns:connection.execute(f'DROP INDEX {quote(name)}')
    for name,columns in desired.items():
        if existing.get(name)!=columns:
            connection.execute(f'CREATE INDEX {quote(name)} ON {quote(data_name)} ('+','.join(quote(c) for c in columns)+')')
