"""Resumable E uploads with disk-backed plans and per-table commit receipts."""
from contextlib import closing
import codecs
import copy
import hashlib
import json
import math
import re
import sqlite3

import msgpack
from core import TableDefinition, SchemaError
from core.definition_files import normalize_definition_labels
from core.stream_efile import events
from core.sqlite_db import _OBJECT, _BOOL, _FLOAT
from core.sqlite_schema import _read_embedded_database, write_embedded_definition
from .atomic_import import create_layout, encode, quote
from .csv_imports import file_hash
from .definition_import import convert, DefinitionImporter
from .errors import APIError
from .sqlite_service import atomic_json, fingerprint, validate_populated_schema
from .tasks import checkpoint, committing, current_task, table_completed, TaskCancelled

MAX_ROWS = 100000
BATCH = 512
RECEIPTS = 'rtdb_web_efile_steps'


def identity(manager, payload):
    if not payload.get('database'):
        raise APIError(400, '请先选择或新建数据库')
    scoped = manager.for_database(payload['database'])
    if scoped.native:
        raise APIError(403, 'E 文件导入不适用于原生 SQLite 库')
    encoding = payload.get('encoding', 'auto')
    if encoding not in ('auto', 'utf-8-sig', 'gb18030', 'utf-16'):
        raise APIError(400, '不支持的 E 文件编码')
    return dict(database=scoped.database_file, encoding=encoding)


def detect(path, encoding):
    with path.open('rb') as source:
        bom = source.read(3)
    candidates = [encoding] if encoding != 'auto' else ['utf-16'] if bom[:2] in (b'\xff\xfe', b'\xfe\xff') else ['utf-8-sig', 'gb18030']
    for candidate in candidates:
        decoder = codecs.getincrementaldecoder(candidate)(errors='strict')
        try:
            with path.open('rb') as source:
                while block := source.read(1024**2):
                    decoder.decode(block); checkpoint('识别 E 文件编码')
                decoder.decode(b'', final=True)
            return candidate
        except UnicodeError:
            pass
    raise APIError(422, '无法解码 E 文件，请清除临时上传并选择正确编码')


def candidates(cell):
    if cell in ('', r'\N'):
        return 15
    mask = 8
    if re.fullmatch(r'-?(?:0|[1-9]\d*)', cell): mask |= 1
    if cell.lower() in ('true', 'false'): mask |= 2
    try:
        if math.isfinite(float(cell)) and not re.fullmatch(r'-?0\d+', cell): mask |= 4
    except ValueError:
        pass
    return mask


def fields(definition):
    return ','.join(f'c{i}' for i in range(len(definition.columns)))


def revision(connection, definition, data_name):
    digest = hashlib.sha256(fingerprint(definition.to_mapping()).encode())
    for index, row in enumerate(connection.execute(f'SELECT _pos,{fields(definition)} FROM {quote(data_name)} ORDER BY _pos')):
        if index % 1000 == 0: checkpoint('核对表版本', index)
        value = msgpack.packb(row, use_bin_type=True)
        digest.update(len(value).to_bytes(8, 'big')); digest.update(value)
    high = connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (data_name,)).fetchone()
    digest.update(msgpack.packb(high))
    return digest.hexdigest()


def decoded(definition, raw):
    result = {}
    for column, cell in zip(definition.columns, raw):
        kind = column.data_type.lower()
        if cell is not None:
            if kind in _OBJECT: cell = msgpack.unpackb(cell, raw=False, strict_map_key=False)
            elif kind in _BOOL: cell = bool(cell)
            elif kind in _FLOAT: cell = float(cell)
        result[column.eng_name] = cell
    return result


def prepare(manager, value):
    from . import database_uploads as uploads
    if value['state'] == 'ready': return value['result']
    if value['state'] == 'error': raise APIError(422, value['error'])
    if value['received'] != value['size']: raise APIError(409, 'E 文件尚未上传完整')
    task = current_task()
    value.update(state='validating', task_id=task['id'] if task else None, task_fingerprint=task['fingerprint'] if task else None)
    uploads._save(manager, value)
    folder = uploads.session_path(manager, value['id']); raw_path = folder/'raw.sqlite3'
    try:
        checksum = uploads.verify(manager, value)
        encoding = detect(folder/'payload.sqlite3', value['encoding'])
        raw_path.unlink(missing_ok=True)
        scoped = manager.for_database(value['database']); tables = []
        with closing(sqlite3.connect(raw_path)) as staging, scoped.read_scope():
            staging.execute('PRAGMA cache_size=-2048')
            staging.execute('CREATE TABLE rows (table_no INTEGER, line INTEGER, cells TEXT)')
            staging.execute('CREATE INDEX row_table ON rows(table_no)')
            definitions = {t['name']:t['definition'] for t in scoped.tables()['tables'] if not t.get('orphan')}
            with (folder/'payload.sqlite3').open('r', encoding=encoding, newline='') as source:
                for event, line, item in events(source):
                    if event == 'start':
                        name = scoped.name(item['name']); count = 0; samples = []; masks = []; present = []
                    elif event == 'columns':
                        masks = [15]*len(item['columns']); present = [False]*len(masks)
                    elif event == 'row':
                        count += 1
                        if count > min(MAX_ROWS, scoped.max_rows): raise APIError(413, f'{name} 超过100,000条记录')
                        if count % 500 == 1: checkpoint(f'解析 {name}', count)
                        staging.execute('INSERT INTO rows VALUES(?,?,?)', (len(tables), line, json.dumps(item, ensure_ascii=False)))
                        for i, cell in enumerate(item):
                            masks[i] &= candidates(cell); present[i] |= cell not in ('', r'\N')
                        if len(samples) < 5: samples.append([cell[:80] for cell in item[:20]])
                    elif event == 'end':
                        old = definitions.get(name)
                        desired = copy.deepcopy(old) if old else dict(head=dict(eng_name=name, chn_name=item['props'].get('name', name)), columns=[])
                        known = {c['eng_name'] for c in desired['columns']}
                        for i, field in enumerate(item['columns']):
                            if field in known: continue
                            kind = 'str' if not present[i] else 'int' if masks[i]&1 else 'bool' if masks[i]&2 else 'float' if masks[i]&4 else 'str'
                            desired['columns'].append(dict(eng_name=field, chn_name=item['descriptions'][i] if i<len(item['descriptions']) else field,
                                data_type=kind, primary_key=not old and i==0, disp_type={'int':'整数','float':'浮点数','bool':'布尔'}.get(kind,'文本'), disp_menu=''))
                        definition = TableDefinition.from_mapping(normalize_definition_labels(desired), scoped.schema_dir, name)
                        tables.append(dict(name=name, definition=definition.to_mapping(), incoming=count, source_fields=item['columns'], sample=samples, old=old))
            staging.commit()
        result = dict(kind='e', id=value['id'], database=value['database'], encoding=encoding, tables=tables, sha256=checksum)
        value.update(state='ready', result=result, raw_sha256=file_hash(raw_path))
        uploads._save(manager, value)
        return result
    except TaskCancelled:
        value.update(state='receiving'); uploads._save(manager,value); raise
    except Exception as error:
        value.update(state='error',error=str(error)); uploads._save(manager,value)
        if isinstance(error,APIError): raise
        raise APIError(422, 'E 文件解析失败：'+str(error)) from error


def session(manager, ident):
    from . import database_uploads as uploads
    value = uploads._load(manager,ident)
    if value.get('kind') != 'e' or value['state'] != 'ready': raise APIError(409,'请先上传并解析 E 文件')
    if manager.database_file != value['database']: raise APIError(409,'当前数据库与上传目标不一致')
    return value, uploads.session_path(manager,ident)


def validate(manager, payload):
    from . import database_uploads as uploads
    ident = payload.get('upload_id')
    with uploads.lock(manager,ident), manager.exclusive():
        manager._recover(); value, folder = session(manager,ident)
        if value.get('apply_started'): raise APIError(409,'该计划已开始提交，请继续未完成步骤；更改选项需重新上传')
        mode = payload.get('mode','merge')
        if mode not in ('append','merge','replace'): raise APIError(400,'未知导入方式')
        if file_hash(folder/'raw.sqlite3') != value['raw_sha256']: raise APIError(422,'暂存 E 数据损坏，请重新上传')
        # Invalid changed choices must never leave an older executable preview.
        value.pop('validation',None); value.pop('plans',None); uploads._save(manager,value)
        prepared = folder/'prepared.sqlite3'; prepared.unlink(missing_ok=True); plans=[]
        with closing(sqlite3.connect(prepared)) as staging, closing(sqlite3.connect((folder/'raw.sqlite3').as_uri()+'?mode=ro',uri=True)) as source:
            staging.execute('PRAGMA cache_size=-2048')
            current={t['name']:t['definition'] for t in manager.tables()['tables'] if not t.get('orphan')}
            for index,item in enumerate(value['result']['tables']):
                plans.append(build_plan(manager,staging,source,index,item,mode,payload.get('definition_overrides'),current.get(item['name'])))
            staging.commit()
        digest=file_hash(prepared)
        token=fingerprint(dict(id=ident,database=manager.database_file,mode=mode,plans=plans,sha256=digest))
        result=dict(token=token,mode=mode,tables=[p['summary'] for p in plans],warnings=[w for p in plans for w in p['summary']['warnings']])
        value.update(plans=plans,prepared_sha256=digest,validation=result)
        uploads._save(manager,value)
        return result


def build_plan(manager, staging, source, index, item, mode, overrides, current):
    name=item['name']; checkpoint(f'校验表 {name}',index)
    desired=DefinitionImporter._apply_efile_overrides(copy.deepcopy(item['definition']),name,overrides)
    definition=TableDefinition.from_mapping(desired,manager.schema_dir,name)
    if current != item['old']: raise APIError(409,f'{name} 结构已变化，请重新上传预览')
    data,meta=f't{index}',f'm{index}';create_layout(staging,definition,data,meta)
    names=list(definition.column_names);keys=[i for i,c in enumerate(definition.columns) if c.primary_key]
    key_fields=','.join(f'c{i}' for i in keys);where=' AND '.join(f'c{i}=?' for i in keys)
    markers=','.join('?' for _ in names); old=None;before=0;version=None;high=0
    if not all(k in item['source_fields'] for k in definition.primary_keys): raise SchemaError(f'{name}：文件必须包含全部 KEY 字段')
    staging.execute(f'CREATE TABLE p{index} AS SELECT _pos,{key_fields} FROM {data} WHERE 0')
    staging.execute(f'CREATE UNIQUE INDEX pk{index} ON p{index}({key_fields})')
    staging.execute(f'CREATE TABLE seen{index} AS SELECT {key_fields} FROM {data} WHERE 0')
    staging.execute(f'CREATE UNIQUE INDEX sk{index} ON seen{index}({key_fields})')
    if current:
        old=TableDefinition.from_mapping(current,manager.schema_dir,name)
        with manager.session(name) as table, table._read_transaction() as connection:
            before=len(table)
            if before>manager.max_rows: raise APIError(413,f'{name} 超过100,000条记录')
            if before: validate_populated_schema(old,definition)
            version=revision(connection,old,table._data_name)
            seq=connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(table._data_name,)).fetchone();high=seq[0] if seq else 0
            cursor=connection.execute(f'SELECT _pos,{fields(old)} FROM {table._data_sql} ORDER BY _pos')
            while batch:=cursor.fetchmany(BATCH):
                checkpoint(f'暂存 {name} 原有数据')
                values=[];positions=[]
                for raw in batch:
                    row=decoded(old,raw[1:]);converted={c.eng_name:convert(row.get(c.eng_name),c) for c in definition.columns}
                    definition.validate_rows([converted]);encoded=tuple(encode(converted[c.eng_name],c) for c in definition.columns)
                    values.append((raw[0],*encoded));positions.append((raw[0],*(encoded[i] for i in keys)))
                staging.executemany(f'INSERT INTO p{index} VALUES({",".join("?" for _ in range(len(keys)+1))})',positions)
                if mode!='replace':staging.executemany(f'INSERT INTO {data} (_pos,{fields(definition)}) VALUES(?,{markers})',values)
    staging.execute('DELETE FROM sqlite_sequence WHERE name=?',(data,))
    staging.execute('INSERT INTO sqlite_sequence VALUES(?,?)',(data,high))
    incoming_fields=[i for i,c in enumerate(definition.columns) if c.eng_name in item['source_fields'] and not c.primary_key]
    assignments=','.join(f'c{i}=excluded.c{i}' for i in incoming_fields)
    command=f'INSERT INTO {data} (_pos,{fields(definition)}) VALUES(?,{markers}) ON CONFLICT ({key_fields}) '+('DO UPDATE SET '+assignments if mode!='append' and assignments else 'DO NOTHING')
    inserted=updated=duplicates=skipped=0
    for number,(line,cells) in enumerate(source.execute('SELECT line,cells FROM rows WHERE table_no=? ORDER BY rowid',(index,)),1):
        if number%500==1:checkpoint(f'校验 {name} 数据',number,item['incoming'])
        row=dict(zip(item['source_fields'],json.loads(cells)))
        try:
            converted={c.eng_name:convert(row[c.eng_name],c,from_file=True) for c in definition.columns if c.eng_name in row}
            definition.validate_rows([converted])
            if any(isinstance(converted[k],str) and not converted[k].strip() for k in definition.primary_keys):raise SchemaError('KEY 不能为空')
            encoded=tuple(encode(converted.get(c.eng_name),c) for c in definition.columns);key=tuple(encoded[i] for i in keys)
            duplicates+=not bool(staging.execute(f'INSERT OR IGNORE INTO seen{index} VALUES({",".join("?" for _ in keys)})',key).rowcount)
            exists=staging.execute(f'SELECT _pos FROM {data} WHERE {where}',key).fetchone()
            if exists:
                if mode=='append': skipped+=1;continue
                updated+=1;position=exists[0]
            else:
                inserted+=1
                previous=staging.execute(f'SELECT _pos FROM p{index} WHERE {where}',key).fetchone()
                position=previous[0] if previous else None
            staging.execute(command,(position,*encoded))
        except (SchemaError,ValueError,TypeError,OverflowError,sqlite3.IntegrityError) as error:
            raise APIError(422,f'{name} 第 {line} 行：{error}') from error
    after=staging.execute(f'SELECT row_count FROM {meta}').fetchone()[0]
    if after>min(MAX_ROWS,manager.max_rows):raise APIError(413,f'{name} 合并后超过100,000条记录')
    warnings=[]
    if duplicates:warnings.append(f'{name}：文件内重复KEY {duplicates} 条；'+('保留首次出现的记录' if mode=='append' else '采用最后一条记录')+'，继续加载')
    if skipped:warnings.append(f'{name}：追加跳过 {skipped} 条主键冲突记录')
    if mode=='replace' and before:warnings.append(f'{name}：替换原有 {before} 条记录')
    summary=dict(name=name,exists=old is not None,before=before,after=after,incoming=item['incoming'],inserted=inserted,updated=updated,
        duplicates=duplicates,skipped=skipped,definition=definition.to_mapping(),primary_keys=list(definition.primary_keys),warnings=warnings)
    staging.execute(f'DROP TABLE p{index}');staging.execute(f'DROP TABLE seen{index}')
    return dict(summary=summary,old=current,version=version,data=data,high=high)


def apply_table(manager,plan,prepared,ident,signature):
    from .write_receipts import record_step
    name=plan['summary']['name'];path=manager.data_dir/manager.database_file
    manager.require_writable(path=path)
    desired=TableDefinition.from_mapping(plan['summary']['definition'],manager.schema_dir,name)
    with closing(sqlite3.connect(path,timeout=10,isolation_level=None)) as connection:
        connection.execute('PRAGMA journal_mode=WAL');connection.execute('PRAGMA synchronous=FULL')
        connection.execute('ATTACH DATABASE ? AS incoming',(prepared.as_uri()+'?mode=ro',))
        connection.execute('BEGIN IMMEDIATE')
        try:
            connection.execute(f'CREATE TABLE IF NOT EXISTS {RECEIPTS}(upload_id TEXT,name TEXT,signature TEXT,result TEXT,PRIMARY KEY(upload_id,name))')
            previous=connection.execute(f'SELECT signature,result FROM {RECEIPTS} WHERE upload_id=? AND name=?',(ident,name)).fetchone()
            if previous:
                if previous[0]!=signature:raise APIError(409,'该上传已经按另一计划提交')
                connection.rollback();return {**json.loads(previous[1]),'replayed':True}
            document=_read_embedded_database(connection,path)
            if document is None:raise APIError(409,'数据库缺少内置定义')
            old=next((table for table in document.tables if table.eng_name.casefold()==name.casefold()),None)
            if (old.to_mapping() if old else None)!=plan['old']:raise APIError(409,f'{name} 结构已变化，请重新上传预览')
            data_name='rtdb_data_'+name if document.is_grouped or old is None else 'data'
            meta_name='rtdb_metadata_'+name if document.is_grouped or old is None else 'metadata'
            if old and revision(connection,old,data_name)!=plan['version']:raise APIError(409,f'{name} 数据已变化，请重新上传预览')
            atomic_json(manager._journal,dict(kind='definition-import',database_path=str(path),schema_path=str(manager.schema_dir/(path.stem+'.json'))))
            if old is None and document.tables and not document.is_grouped:
                legacy=document.tables[0].eng_name;promoted='rtdb_data_'+legacy
                connection.execute(f'ALTER TABLE data RENAME TO {quote(promoted)}')
                connection.execute(f'ALTER TABLE metadata RENAME TO {quote("rtdb_metadata_"+legacy)}')
                for kind,obj,sql in connection.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",(promoted,)).fetchall():
                    connection.execute(f'DROP {kind} {quote(obj)}');connection.execute(sql.replace(quote(obj),quote(promoted+'_'+obj),1))
            layout=lambda definition:[(c.eng_name,c.data_type,c.primary_key) for c in definition.columns]
            structural=not old or layout(old)!=layout(desired)
            data=quote(data_name);metadata=quote(meta_name);source='incoming.'+quote(plan['data'])
            if structural:
                if old:connection.execute(f'DROP TABLE {data}');connection.execute(f'DROP TABLE {metadata}')
                create_layout(connection,desired,data_name,meta_name)
                connection.execute(f'INSERT INTO {data} (_pos,{fields(desired)}) SELECT _pos,{fields(desired)} FROM {source} ORDER BY _pos')
            else:
                connection.execute(f'DELETE FROM {data} WHERE _pos NOT IN (SELECT _pos FROM {source})')
                assignments=','.join(f'c{i}=excluded.c{i}' for i in range(len(desired.columns)))
                changed=' OR '.join(f'{data}.c{i} IS NOT excluded.c{i}' for i in range(len(desired.columns)))
                connection.execute(f'INSERT INTO {data} (_pos,{fields(desired)}) SELECT _pos,{fields(desired)} FROM {source} WHERE 1 ON CONFLICT(_pos) DO UPDATE SET {assignments} WHERE {changed}')
            if connection.execute('SELECT 1 FROM sqlite_sequence WHERE name=?',(data_name,)).fetchone():
                connection.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?',(plan['high'],data_name))
            else:connection.execute('INSERT INTO sqlite_sequence VALUES(?,?)',(data_name,plan['high']))
            connection.execute(f'UPDATE {metadata} SET fingerprint=?',(fingerprint(desired.to_mapping()),))
            write_embedded_definition(connection,desired,path.stem,document.chn_name)
            from core.sqlite_indexes import reconcile_indexes
            reconcile_indexes(connection,data_name,desired)
            summary=plan['summary'];result=dict(name=name,ok=True,rows=summary['after'],replayed=False,
                **{k:summary[k] for k in ('incoming','inserted','updated','duplicates','skipped','warnings')})
            connection.execute(f'INSERT INTO {RECEIPTS} VALUES(?,?,?,?)',(ident,name,signature,json.dumps(result,ensure_ascii=False)))
            record_step(connection,name,result);connection.commit()
        except BaseException:
            connection.rollback();raise
    try:manager._recover()
    except Exception as error:result['export_warning']=f'{name} 已提交，定义导出待恢复：{error}'
    return result


def apply(manager,payload):
    from . import database_uploads as uploads
    ident=payload.get('upload_id')
    with uploads.lock(manager,ident),manager.exclusive():
        manager._recover();value,folder=session(manager,ident);validation=value.get('validation') or {}
        if payload.get('token')!=validation.get('token') or not validation:raise APIError(409,'请先校验当前字段选择')
        if file_hash(folder/'prepared.sqlite3')!=value['prepared_sha256']:raise APIError(422,'导入计划损坏，请重新上传')
        value['apply_started']=True;uploads._save(manager,value);results=[]
        for index,plan in enumerate(value['plans']):
            checkpoint(f'准备提交 {plan["summary"]["name"]}',index,len(value['plans']))
            try:
                with committing(f'提交 {plan["summary"]["name"]}；单表失败整体回滚'):
                    result=apply_table(manager,plan,folder/'prepared.sqlite3',ident,validation['token'])
                    table_completed(plan['summary']['name'])
                results.append(result)
            except TaskCancelled:raise
            except Exception as error:
                results.append(dict(name=plan['summary']['name'],ok=False,error=str(error)))
                return dict(ok=False,results=results,pending=[p['summary']['name'] for p in value['plans'][index+1:]],retryable=True)
        return dict(ok=True,results=results,pending=[],warnings=validation['warnings'])
