"""Bounded CSV validation on disk, followed by an atomic table import."""
from contextlib import closing
import csv
import hashlib
import json
import math
import sqlite3
import msgpack

from core.sqlite_db import _INT, _FLOAT, _BOOL, _OBJECT
from .errors import APIError
from .sqlite_service import fingerprint
from .tasks import checkpoint, committing, current_task, TaskCancelled
from .atomic_import import create_layout
from . import write_receipts

BATCH=512
MAX_ROWS=100000
MAX_BYTES=128*1024**2


def identity(manager,payload):
    database=payload.get('database');table=manager.name(payload.get('table'))
    if not database:raise APIError(400,'CSV 导入需要明确选择数据库和表')
    scoped=manager.for_database(database);scoped.definition(table)
    encoding=payload.get('encoding','utf-8-sig')
    if encoding=='utf-8':encoding='utf-8-sig'
    if encoding not in ('utf-8-sig','gb18030'):raise APIError(400,'CSV 编码仅支持 UTF-8 / GB18030')
    return dict(database=scoped.database_file,table=table,encoding=encoding)


def revision(table,connection):
    """Hash a stable ordered snapshot without materializing the table."""
    digest=hashlib.sha256(fingerprint(table.definition.to_mapping()).encode())
    fields=','.join(table._fields.values())
    for number,row in enumerate(connection.execute(f'SELECT _pos,{fields} FROM {table._data_sql} ORDER BY _pos')):
        if number%1000==0:checkpoint('核对目标表版本',number)
        value=msgpack.packb(row,use_bin_type=True)
        digest.update(len(value).to_bytes(8,'big'));digest.update(value)
    return digest.hexdigest()


def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024**2):
            digest.update(block);checkpoint('核对暂存导入内容')
    return digest.hexdigest()


def prepare(manager,value):
    from . import database_uploads as uploads
    if value['state']=='ready':return value['result']
    if value['state']=='error':raise APIError(422,value.get('error','请放弃后重新上传'))
    if value['received']!=value['size']:raise APIError(409,'CSV 文件尚未上传完整')
    task=current_task();value.update(state='validating',task_id=task['id'] if task else None,task_fingerprint=task['fingerprint'] if task else None)
    uploads._save(manager,value)
    folder=uploads.session_path(manager,value['id']);prepared=folder/'prepared.sqlite3'
    try:
        actual=uploads.verify(manager,value)
        # This is our disposable staging table; a cancelled preview is rebuilt.
        prepared.unlink(missing_ok=True)
        scoped=manager.for_database(value['database'])
        with scoped.session(value['table']) as table, closing(sqlite3.connect(prepared)) as staging:
            staging.execute('PRAGMA cache_size=-2048');staging.execute('PRAGMA synchronous=FULL')
            create_layout(staging,table.definition,'rows','metadata')
            fields=','.join(table._fields.values());markers=','.join('?' for _ in table.columns)
            sample=[];count=0
            with (folder/'payload.sqlite3').open('r',encoding=value['encoding'],errors='strict',newline='') as stream:
                reader=csv.DictReader(stream,strict=True)
                names=reader.fieldnames
                if not names or len(names)!=len(set(names)) or set(names)!=set(table.columns):
                    raise APIError(422,'CSV 须包含全部英文字段名，不能重复或包含未知字段')
                try:
                    for source in reader:
                        count+=1
                        if count>min(MAX_ROWS,scoped.max_rows):raise APIError(413,'CSV 超过 100,000 条记录')
                        if count%500==1:checkpoint('逐行校验 CSV',count)
                        if None in source or None in source.values():raise ValueError('列数与表头不一致')
                        row={}
                        for name,cell in source.items():
                            kind=table._types[name]
                            if len(cell)>131072:raise ValueError(f'{name} 单元格超过128 Ki字符')
                            if cell==r'\N':cell=None
                            elif kind in _INT:cell=int(cell)
                            elif kind in _FLOAT:
                                cell=float(cell)
                                if not math.isfinite(cell):raise ValueError(f'{name} 必须为有限数值')
                            elif kind in _BOOL|_OBJECT:cell=json.loads(cell)
                            elif cell.startswith('\\\\'):cell=cell[1:]
                            row[name]=cell
                        row=scoped._coerce(table,row,complete=True);table.definition.validate_rows([row])
                        if any(isinstance(row[key],str) and not row[key].strip() for key in table.primary_keys):
                            raise ValueError('KEY 不能为空')
                        values=tuple(table._encode(name,row[name]) for name in table.columns)
                        staging.execute(f'INSERT INTO rows ({fields}) VALUES ({markers})',values)
                        if len(sample)<20:
                            sample.append({key:(cell[:160]+'…' if len(cell)>160 else cell) if isinstance(cell,str)
                                else json.dumps(cell,ensure_ascii=False)[:160] if isinstance(cell,(dict,list)) else cell for key,cell in row.items()})
                except (ValueError,TypeError,csv.Error,sqlite3.IntegrityError) as error:
                    raise APIError(422,f'CSV 第 {reader.line_num} 行：{error}（请检查值、列数及重复 KEY）') from error
            if not count:raise APIError(422,'CSV 没有可导入记录')
            staging.commit()
            with table._read_transaction() as connection:version=revision(table,connection)
            schema=fingerprint(table.definition.to_mapping())
            result=dict(kind='csv',id=value['id'],database=value['database'],table=value['table'],encoding=value['encoding'],
                rows=count,sample=sample,columns=[{'name':c.eng_name,'type':c.data_type,'key':c.primary_key} for c in table.definition.columns],
                schema=schema,version=version,sha256=actual)
        result['prepared_sha256']=file_hash(prepared)
        result['token']=fingerprint(result)
        value.update(state='ready',result=result);uploads._save(manager,value)
        return result
    except TaskCancelled:
        value.update(state='receiving');uploads._save(manager,value);raise
    except Exception as error:
        value.update(state='error',error=str(error));uploads._save(manager,value)
        if isinstance(error,APIError):raise
        raise APIError(422,'CSV 校验失败：'+str(error)) from error


def apply(manager,name,payload):
    from . import database_uploads as uploads
    mode=payload.get('mode','append')
    if mode not in ('append','merge','replace'):raise APIError(400,'未知导入方式')
    if mode!='append' and payload.get('confirm')!=name:raise APIError(400,'合并或替换需要输入表名确认')
    ident=payload.get('upload_id')
    with uploads.lock(manager,ident):
        value=uploads._load(manager,ident);preview=value.get('result') or {}
        if value.get('kind')!='csv' or value['state']!='ready':raise APIError(409,'请先完成 CSV 校验预览')
        if value['database']!=manager.database_file or value['table']!=name:raise APIError(409,'上传文件绑定的数据库或表与当前目标不同')
        if preview.get('token')!=payload.get('token'):raise APIError(409,'CSV 预览编号不匹配，请重新预览')
        prepared=uploads.session_path(manager,ident)/'prepared.sqlite3'
        if not prepared.is_file() or file_hash(prepared)!=preview['prepared_sha256']:raise APIError(422,'暂存 CSV 已损坏，请重新上传')
        signature=fingerprint({'token':preview['token'],'mode':mode})
        with manager.session(name) as table, closing(sqlite3.connect(prepared.as_uri()+'?mode=ro',uri=True)) as source:
            if fingerprint(table.definition.to_mapping())!=preview['schema']:raise APIError(409,'表结构已变化，请重新上传校验')
            fields=','.join(table._fields.values());markers=','.join('?' for _ in table.columns)
            command=f'INSERT INTO {table._data_sql} ({fields}) VALUES ({markers})'
            if mode=='merge':
                keys=','.join(table._fields[key] for key in table.primary_keys)
                assignments=','.join(f'{physical}=excluded.{physical}' for key,physical in table._fields.items() if key not in table.primary_keys)
                command+=f' ON CONFLICT ({keys}) '+('DO UPDATE SET '+assignments if assignments else 'DO NOTHING')
            def commit(connection):
                connection.execute('CREATE TABLE IF NOT EXISTS rtdb_web_csv_imports (id TEXT PRIMARY KEY, signature TEXT NOT NULL, result TEXT NOT NULL)')
                previous=connection.execute('SELECT signature,result FROM rtdb_web_csv_imports WHERE id=?',(ident,)).fetchone()
                if previous:
                    if previous[0]!=signature:raise APIError(409,'该 CSV 已按另一方式导入，请勿重复执行')
                    return {**json.loads(previous[1]),'replayed':True}
                if mode=='replace':
                    if revision(table,connection)!=preview['version']:raise APIError(409,'预览后表数据已变化，请重新上传并确认替换')
                    connection.execute(f'DELETE FROM {table._data_sql}')
                cursor=source.execute(f'SELECT {fields} FROM rows ORDER BY _pos');done=0
                while batch:=cursor.fetchmany(BATCH):
                    connection.executemany(command,batch);done+=len(batch)
                    checkpoint('提交 CSV 记录',done,preview['rows'])
                if connection.execute(f'SELECT row_count FROM {table._metadata_sql}').fetchone()[0]>manager.max_rows:
                    raise APIError(413,'导入后超过100,000条记录，本次已回滚')
                result=dict(imported=done,mode=mode,upload_id=ident,replayed=False)
                connection.execute('INSERT INTO rtdb_web_csv_imports VALUES(?,?,?)',(ident,signature,json.dumps(result)))
                return result
            checkpoint('CSV 校验完成，准备提交',0,preview['rows'])
            with committing('单事务提交 CSV；失败将整体回滚'):
                return write_receipts.write(table,commit)
