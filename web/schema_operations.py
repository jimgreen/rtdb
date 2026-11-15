"""Scoped schema creation/deletion with receipts in the same business transaction."""
from contextlib import closing
import sqlite3
import uuid

from core.sqlite_schema import _read_embedded_database, write_embedded_definition, remove_embedded_definition
from .atomic_import import create_layout, quote
from .errors import APIError
from . import write_receipts


def query_plan(manager, name, payload):
    statement=payload.get('sql','')
    if not isinstance(statement,str) or not statement.strip() or len(statement)>100000 or statement.strip().split(None,1)[0].upper() not in ('SELECT','WITH'):
        raise APIError(400,'查询计划仅支持 SELECT / WITH 语句')
    with manager.session(name) as table:
        manager._view(table,table._connection())
        with table._read_transaction() as connection,manager._reader(table,connection):
            plan=connection.execute('EXPLAIN QUERY PLAN '+statement.strip().removesuffix(';')).fetchall()
        return dict(plan=[dict(id=row[0],parent=row[1],detail=row[3]) for row in plan],
                    columns=table._fields,indexes=[list(fields) for fields in table.definition.secondary_indexes])


def preview(manager, name, payload):
    from core import TableDefinition
    from core.definition_files import normalize_definition_labels
    from .sqlite_service import fingerprint, _normalise_column_renames, _schema_rows
    desired=TableDefinition.from_mapping(normalize_definition_labels(payload.get('definition')),manager.schema_dir)
    manager.name(desired.eng_name)
    changes=[];errors=[];count=0
    with manager.read_scope():
        if payload.get('creating'):
            if manager._find(desired.eng_name):errors.append('表名已存在')
            changes=[f'新建表：{desired.chn_name} / {desired.eng_name}',f'字段：{", ".join(desired.column_names)}']
        else:
            old=manager.definition(name)
            if payload.get('version')!=fingerprint(old.to_mapping()):raise APIError(409,'表结构已变化，请刷新后预览')
            with manager.session(name) as table:count=len(table)
            renames=_normalise_column_renames(old,desired,payload.get('column_renames'))
            previous={renames.get(c.eng_name,c.eng_name):c for c in old.columns}
            current={c.eng_name:c for c in desired.columns}
            if desired.head!=old.head:changes.append(f'表信息：{old.chn_name} / {old.eng_name} → {desired.chn_name} / {desired.eng_name}')
            for before,after in renames.items():changes.append(f'字段改名：{before} → {after}')
            for field in current.keys()-previous.keys():changes.append(f'增加字段：{field}')
            for field in previous.keys()-current.keys():changes.append(f'删除字段及其数据：{field}')
            for field in current.keys()&previous.keys():
                a,b=previous[field],current[field]
                if a.data_type!=b.data_type:
                    changes.append(f'字段类型：{field} {a.data_type} → {b.data_type}')
                if a.chn_name!=b.chn_name or a.disp_type!=b.disp_type or a.disp_menu!=b.disp_menu or a.default_value!=b.default_value:changes.append(f'字段属性：{field}')
            if tuple(renames.get(k,k) for k in old.primary_keys)!=desired.primary_keys:
                changes.append('修改主键组合')
            old_order=[renames.get(k,k) for k in old.column_names if renames.get(k,k) in current]
            new_order=[k for k in desired.column_names if k in previous]
            if old_order!=new_order:
                changes.append('调整已有字段顺序')
            if old.indexes!=desired.indexes:changes.append('辅助索引：'+('自动逐列' if desired.indexes is None else '；'.join(' → '.join(fields) for fields in desired.indexes) or '仅保留主键索引'))
            layout=lambda table:[(c.eng_name,c.data_type,c.primary_key) for c in table.columns]
            if count and layout(old)!=layout(desired):
                try:
                    with manager.session(name) as table,table._read_transaction() as connection:_schema_rows(table,connection,old,desired,renames)
                except (APIError,ValueError) as error:errors.append(str(error))
    return dict(changes=changes,errors=errors,row_count=count,valid=not errors)


def export_after_commit(manager, result):
    try:manager._recover()
    except Exception as error:
        result={**result,'export_warning':f'SQLite 已提交，定义文件导出待恢复：{error}'}
    return result


def create(manager, desired):
    from .sqlite_service import atomic_json, fingerprint
    path=manager.data_dir/manager.database_file
    with closing(sqlite3.connect(path,timeout=10,isolation_level=None)) as connection:
        connection.execute('PRAGMA journal_mode=WAL');connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        try:
            def apply(c):
                document=_read_embedded_database(c,path)
                if document is None:raise APIError(409,'数据库缺少内置定义，请先修复')
                if any(table.eng_name==desired.eng_name for table in document.tables):raise APIError(409,'表名已存在')
                if document.tables and not document.is_grouped:raise APIError(409,'数据库需要先转换为多表布局')
                atomic_json(manager._journal,dict(kind='definition-import',database_path=str(path),schema_path=str(manager.schema_dir/(path.stem+'.json'))))
                create_layout(c,desired,'rtdb_data_'+desired.eng_name,'rtdb_metadata_'+desired.eng_name)
                write_embedded_definition(c,desired,path.stem,document.chn_name)
                return dict(name=desired.eng_name,definition=desired.to_mapping(),schema_version=fingerprint(desired.to_mapping()))
            result=write_receipts.in_transaction(connection,apply)
            connection.commit();write_receipts.mark_committed(result)
        except BaseException:connection.rollback();raise
    return export_after_commit(manager,result)


def drop(manager, name, payload):
    from .sqlite_service import atomic_json, fingerprint
    with manager.exclusive():
        manager._recover()
        with manager._database() as database:
            table=database[name]
            if payload.get('version')!=fingerprint(table.definition.to_mapping()):raise APIError(409,'表结构已变化，请刷新')
            archive=manager.data_dir/'.web-archive'/(name+'-'+uuid.uuid4().hex)
            archive.mkdir(parents=True)
            with closing(sqlite3.connect(archive/table.path.name)) as backup:table._connection().backup(backup)
            def remove(connection):
                if connection.execute(f'SELECT 1 FROM {table._data_sql} LIMIT 1').fetchone():raise APIError(409,'表有数据，请先导出并清空后删除表结构')
                document=_read_embedded_database(connection,table.path)
                atomic_json(manager._journal,dict(kind='definition-import',database_path=str(table.path),schema_path=str(manager.schema_dir/(table.path.stem+'.json'))))
                connection.execute(f'DROP TABLE {table._data_sql}')
                connection.execute(f'DROP TABLE {table._metadata_sql}')
                remove_embedded_definition(connection,name)
                return dict(deleted=True,archive=str(archive),database_file=table.path.name,remaining_tables=[t.eng_name for t in document.tables if t.eng_name!=name])
            result=write_receipts.write(table,remove)
        return export_after_commit(manager,result)
