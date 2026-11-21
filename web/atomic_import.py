"""Apply one imported table in one SQLite transaction, preserving sibling tables."""
from contextlib import closing
import re
import sqlite3

from core import TableDefinition
from core.sqlite_db import _INT, _FLOAT, _BOOL, _TEXT, _OBJECT
from core.sqlite_schema import _read_embedded_database, write_embedded_definition
from .errors import APIError
from .sqlite_service import atomic_json, fingerprint


def quote(value):
    return '"'+value.replace('"','""')+'"'


def rename_ddl_object(sql, kind, old_name, new_name):
    """重写 SQLite CREATE INDEX/TRIGGER 语句中的对象名，兼容引号与裸标识符。"""
    pattern = re.compile(
        rf'^(\s*CREATE\s+(?:UNIQUE\s+)?{re.escape(kind)}\s+(?:IF\s+NOT\s+EXISTS\s+)?)'
        rf'(?:"{re.escape(old_name.replace(chr(34), chr(34)*2))}"|`{re.escape(old_name)}`|\[{re.escape(old_name)}\]|{re.escape(old_name)}(?=\s))',
        re.IGNORECASE,
    )
    updated, count = pattern.subn(lambda match: match.group(1) + quote(new_name), sql, count=1)
    if count:
        return updated
    return sql.replace(quote(old_name), quote(new_name), 1)


def encode(value, column):
    if value is None: return None
    kind=column.data_type.lower()
    if kind in _OBJECT:
        import msgpack
        return msgpack.packb(value,use_bin_type=True)
    if kind in _BOOL: return int(value)
    if kind in _FLOAT: return float(value)
    return value


def create_layout(connection, definition, data_name, metadata_name, *, defer_indexes=False):
    data, metadata=quote(data_name),quote(metadata_name)
    fields=[]
    for i,column in enumerate(definition.columns):
        kind=column.data_type.lower()
        affinity='INTEGER' if kind in _INT|_BOOL else 'REAL' if kind in _FLOAT else 'TEXT' if kind in _TEXT else 'BLOB'
        fields.append(f'c{i} {affinity}'+(' NOT NULL' if column.primary_key else ''))
    connection.execute(f'CREATE TABLE {data} (_pos INTEGER PRIMARY KEY AUTOINCREMENT,'+','.join(fields)+')')
    connection.execute(f'CREATE TABLE {metadata} (version INTEGER PRIMARY KEY CHECK(version=1), fingerprint TEXT NOT NULL, row_count INTEGER NOT NULL)')
    connection.execute(f'INSERT INTO {metadata} VALUES (1,?,0)',(fingerprint(definition.to_mapping()),))
    keys=','.join(f'c{i}' for i,c in enumerate(definition.columns) if c.primary_key)
    connection.execute(f'CREATE UNIQUE INDEX {quote(data_name+"_primary_key")} ON {data} ({keys})')
    if not defer_indexes:
        finalize_layout(connection, definition, data_name, metadata_name)


def finalize_layout(connection, definition, data_name, metadata_name):
    """大批量插入完成后统一建立辅助索引、计数触发器并同步 row_count。"""
    data, metadata = quote(data_name), quote(metadata_name)
    from core.sqlite_indexes import reconcile_indexes
    reconcile_indexes(connection, data_name, definition)
    for operation, delta in [('INSERT', '+1'), ('DELETE', '-1')]:
        trigger = quote(data_name + '_count_' + operation.lower())
        connection.execute(f'DROP TRIGGER IF EXISTS {trigger}')
        connection.execute(
            f'CREATE TRIGGER {trigger} AFTER {operation} ON {data} '
            f'BEGIN UPDATE {metadata} SET row_count=row_count{delta}; END'
        )
    count = connection.execute(f'SELECT COUNT(*) FROM {data}').fetchone()[0]
    connection.execute(f'UPDATE {metadata} SET row_count=? WHERE version=1', (count,))


def decode_rows(connection, data_name, definition):
    rows=[]; positions={}
    fields=','.join(f'c{i}' for i in range(len(definition.columns)))
    for raw in connection.execute(f'SELECT _pos,{fields} FROM {quote(data_name)} ORDER BY _pos'):
        row={}
        for column,value in zip(definition.columns,raw[1:]):
            kind=column.data_type.lower()
            if value is not None:
                if kind in _OBJECT:
                    import msgpack
                    value=msgpack.unpackb(value,raw=False,strict_map_key=False)
                elif kind in _BOOL: value=bool(value)
                elif kind in _FLOAT: value=float(value)
            row[column.eng_name]=value
        rows.append(row); positions[tuple(row[k] for k in definition.primary_keys)]=raw[0]
    return rows,positions


def apply_plan(manager, plan):
    name=plan['summary']['name']
    path=(manager.data_dir / manager.database_file) if manager.database_file else manager.database_path(name)
    manager.require_writable(path=path)
    schema_path=manager.schema_dir / (path.stem+'.json')
    desired=TableDefinition.from_mapping(plan['definition'],manager.schema_dir,name)
    with closing(sqlite3.connect(path,timeout=10,isolation_level=None)) as connection:
        connection.execute('PRAGMA journal_mode=WAL'); connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        try:
            document=_read_embedded_database(connection,path)
            if document is None: raise APIError(409,'数据库缺少内置结构，请先加载迁移')
            old=next((table for table in document.tables if table.eng_name.casefold()==name.casefold()),None)
            if old is not None and old.eng_name != name:
                raise APIError(409,f'目标表名与已有表仅大小写不同：{old.eng_name}')
            if (old.to_mapping() if old else None) != plan['old']:
                raise APIError(409,f'{name} 结构已变化，请重新预览')
            data_name='rtdb_data_'+name if document.is_grouped else 'data'
            metadata_name='rtdb_metadata_'+name if document.is_grouped else 'metadata'
            existing, positions=decode_rows(connection,data_name,old) if old else ([],{})
            if old and fingerprint({'definition':old.to_mapping(),'rows':existing}) != plan['version']:
                raise APIError(409,f'{name} 数据已变化，请重新预览')
            atomic_json(manager._journal,{'kind':'definition-import','database_path':str(path),'schema_path':str(schema_path)})
            # A legacy single-table file becomes grouped only when another table is added.
            if old is None and document.tables and not document.is_grouped:
                legacy=document.tables[0].eng_name
                promoted='rtdb_data_'+legacy
                connection.execute(f'ALTER TABLE data RENAME TO {quote(promoted)}')
                connection.execute(f'ALTER TABLE metadata RENAME TO {quote("rtdb_metadata_"+legacy)}')
                for kind,obj,sql in connection.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",(promoted,)).fetchall():
                    connection.execute(f'DROP {kind} {quote(obj)}')
                    connection.execute(rename_ddl_object(sql, kind, obj, promoted + '_' + obj))
            if old is None:
                data_name,metadata_name='rtdb_data_'+name,'rtdb_metadata_'+name
            data,metadata=quote(data_name),quote(metadata_name)
            layout=lambda table:[(c.eng_name,c.data_type,c.primary_key) for c in table.columns]
            structural=old is None or layout(old)!=layout(desired)
            same_primary=old is not None and old.primary_keys==desired.primary_keys
            old_by_key={tuple(row[k] for k in desired.primary_keys):row for row in existing} if same_primary else {}
            new_by_key={tuple(row[k] for k in desired.primary_keys):row for row in plan['retained']}
            if structural:
                high=connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(data_name,)).fetchone() if old else None
                if old:
                    connection.execute(f'DROP TABLE {data}'); connection.execute(f'DROP TABLE {metadata}')
                create_layout(connection,desired,data_name,metadata_name,defer_indexes=True)
                if high:
                    connection.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)',(data_name,high[0]))
                old_by_key={}
            fields=','.join(f'c{i}' for i in range(len(desired.columns)))
            markers=','.join('?' for _ in desired.columns)
            key_fields=[f'c{i}' for i,c in enumerate(desired.columns) if c.primary_key]
            where=' AND '.join(field+' IS ?' for field in key_fields)
            if structural:
                with_pos, without_pos = [], []
                for key, row in new_by_key.items():
                    values = tuple(encode(row.get(c.eng_name), c) for c in desired.columns)
                    if same_primary and key in positions:
                        with_pos.append((positions[key], *values))
                    else:
                        without_pos.append(values)
                if with_pos:
                    connection.executemany(f'INSERT INTO {data} (_pos,{fields}) VALUES (?,{markers})', with_pos)
                if without_pos:
                    connection.executemany(f'INSERT INTO {data} ({fields}) VALUES ({markers})', without_pos)
                finalize_layout(connection, desired, data_name, metadata_name)
            else:
                for key in old_by_key.keys()-new_by_key.keys():
                    connection.execute(f'DELETE FROM {data} WHERE {where}',key)
                for key,row in new_by_key.items():
                    values=tuple(encode(row.get(c.eng_name),c) for c in desired.columns)
                    if key not in old_by_key:
                        connection.execute(f'INSERT INTO {data} ({fields}) VALUES ({markers})',values)
                    elif row != old_by_key[key]:
                        connection.execute(f'UPDATE {data} SET '+','.join(f'c{i}=?' for i in range(len(values)))+' WHERE '+where,(*values,*key))
                from core.sqlite_indexes import reconcile_indexes
                reconcile_indexes(connection,data_name,desired)
            connection.execute(f'UPDATE {metadata} SET fingerprint=? WHERE version=1',(fingerprint(desired.to_mapping()),))
            write_embedded_definition(connection,desired,path.stem,document.chn_name)
            from .write_receipts import record_step
            record_step(connection,name,dict(name=name,ok=True,rows=len(plan['retained'])))
            connection.commit()
        except BaseException:
            connection.rollback()
            manager._journal.unlink(missing_ok=True)
            raise
    # SQLite is authoritative. This journal can regenerate both export files after a crash.
    try:
        manager._recover()
    except Exception as error:
        raise APIError(500,f'{name} 的结构和数据已提交，但 JSON/Python 导出失败：{error}',details={'committed':True}) from error
    return len(plan['retained'])
