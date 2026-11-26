"""Disk-backed, resumable SQLite uploads with bounded binary chunks."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .errors import APIError
from .sqlite_runtime import file_fence
from .sqlite_service import atomic_json
from . import database_snapshots as snapshots
from .tasks import checkpoint, committing, current_task, TaskCancelled
from contextlib import contextmanager
from .storage_maintenance import activity, fence, upload_expired

CHUNK = 1024 * 1024
MAX_BYTES = 2 * 1024**3
MAX_SESSIONS = 8
MAX_RESERVED = 8 * 1024**3
TTL = 24 * 3600


def directory(manager):
    root=manager.data_dir/'.web-uploads';root.mkdir(exist_ok=True)
    return root


def session_path(manager, ident):
    root=directory(manager);path=root/snapshots.identifier(ident)
    if path.resolve().parent != root.resolve(): raise APIError(400,'上传目录无效')
    return path


@contextmanager
def lock(manager, ident):
    with fence(manager), file_fence(directory(manager)/(snapshots.identifier(ident)+'.lock'),True):
        yield


def _load(manager, ident, expired=False):
    path=session_path(manager,ident)/'state.json'
    if not path.is_file(): raise APIError(404,'上传会话不存在，请重新选择文件')
    value=json.loads(path.read_text(encoding='utf-8'))
    if not expired and time.time()-value['updated']>TTL: raise APIError(410,'上传会话已超过24小时，请重新上传')
    return value


def _save(manager, value):
    value['updated']=time.time()
    atomic_json(session_path(manager,value['id'])/'state.json',value)


def _public(value, chunks=False):
    result={key:value[key] for key in ('id','filename','size','last_modified','received','state','sha256','updated')}
    result.update(chunk_size=CHUNK,expires_at=value['updated']+TTL)
    if chunks:result['chunks']=list(value['chunks'])
    for key in ('kind','database','table','encoding'):
        if key in value:result[key]=value[key]
    for key in ('error','result','validation','apply_started'):
        if key in value:result[key]=value[key]
    return result


def status(manager, ident):
    return _public(_load(manager,ident),True)


def _remove(manager, ident):
    folder=session_path(manager,ident)
    for name in ('payload.db','payload.db-wal','payload.db-shm','prepared.db','prepared.db-journal','raw.db','raw.db-journal','state.json'):
        (folder/name).unlink(missing_ok=True)
    # Never recursively remove unrecognized files or follow directories supplied by clients.
    if folder.exists() and not any(folder.iterdir()):folder.rmdir()


@activity
def create(manager, payload):
    ident=snapshots.identifier(payload.get('id'))
    filename=payload.get('filename');size=payload.get('size');modified=payload.get('last_modified',0)
    kind=payload.get('kind','snapshot')
    if kind not in ('snapshot','csv','e'):raise APIError(400,'不支持的上传类型')
    extension={'csv':'.csv','e':'.e'}.get(kind,'.db')
    if not isinstance(filename,str) or len(filename)>255 or not filename.lower().endswith(extension) or len(filename)<=len(extension) or re.search(r'[\\/\x00-\x1f]',filename):
        raise APIError(400,'请选择单个 '+extension+' 文件')
    limit=128*1024**2 if kind in ('csv','e') else MAX_BYTES
    if type(size) is not int or size<=0 or size>limit:raise APIError(413,'CSV/E 上限128 MiB，整库快照上限2 GiB，文件不能为空')
    if type(modified) is not int or modified<0:raise APIError(400,'无效的文件修改时间')
    expected=payload.get('sha256') or ''
    if not isinstance(expected,str) or expected and not re.fullmatch('[a-fA-F0-9]{64}',expected):raise APIError(400,'SHA-256应为64位十六进制字符串')
    identity=dict(filename=filename,size=size,last_modified=modified,sha256=expected.lower())
    if kind=='csv':
        from .csv_imports import identity as csv_identity
        identity.update(kind='csv',**csv_identity(manager,payload))
    if kind=='e':
        from .efile_imports import identity as e_identity
        identity.update(kind='e',**e_identity(manager,payload))
    with file_fence(directory(manager)/'.catalog.lock',True):
        folder=session_path(manager,ident)
        if (folder/'state.json').exists():
            try:
                existing=_load(manager,ident,expired=True)
            except (APIError,OSError,ValueError,KeyError,TypeError):
                existing=None
            if existing is not None:
                if time.time()-existing.get('updated',0)>TTL:
                    if existing.get('state')!='validating' and not existing.get('apply_started'):
                        with lock(manager,ident):
                            _remove(manager,ident)
                    else:
                        raise APIError(410,'上传会话已超过24小时，请重新上传')
                else:
                    if existing.get('kind','snapshot')!=kind or any(existing.get(key)!=value for key,value in identity.items()):
                        raise APIError(409,'上传编号已对应另一文件或目标')
                    return _public(existing,True)
        active=[]
        for item in directory(manager).glob('*/state.json'):
            if not re.fullmatch('[a-f0-9]{32}',item.parent.name):continue
            try:
                value=_load(manager,item.parent.name,expired=True)
            except (APIError,OSError,ValueError,KeyError,TypeError):
                continue
            if upload_expired(manager,value):
                with lock(manager,value['id']):
                    try:
                        fresh=_load(manager,value['id'],expired=True)
                    except (APIError,OSError,ValueError,KeyError,TypeError):
                        continue
                    if upload_expired(manager,fresh):
                        _remove(manager,value['id'])
                        manager.record_operation(category='system',operation='清理过期临时上传',status='success',http_status=200,
                            method='POST',path='/api/manage/database-uploads',result={'filename':fresh['filename'],'bytes':fresh['received']})
                    elif fresh['state']!='ready':active.append(fresh)
            elif value['state']!='ready':active.append(value)
        if len(active)>=MAX_SESSIONS or sum(value['size'] for value in active)+size>MAX_RESERVED:
            raise APIError(429,'未完成上传数量或预留空间已满，请先放弃不需要的上传')
        folder.mkdir(exist_ok=True)
        (folder/'payload.db').write_bytes(b'')
        value=dict(id=ident,**identity,received=0,chunks=[],state='receiving',updated=time.time())
        _save(manager,value)
        return _public(value,True)


def put_chunk(manager, ident, offset, block, digest):
    if type(offset) is not int or offset<0 or offset%CHUNK:raise APIError(400,'无效的分块位置')
    if not isinstance(block,bytes) or not 0<len(block)<=CHUNK:raise APIError(413,'每个分块必须为1字节至1 MiB')
    actual=hashlib.sha256(block).hexdigest()
    if not isinstance(digest,str) or digest.lower()!=actual:raise APIError(422,'分块SHA-256校验失败，请重试当前块')
    with lock(manager,ident):
        value=_load(manager,ident)
        if value['state']!='receiving':raise APIError(409,'当前上传不接受分块，请查询会话状态')
        expected=min(CHUNK,value['size']-offset)
        if len(block)!=expected or offset>value['received']:raise APIError(409,'分块长度或顺序不正确',details={'received':value['received']})
        index=offset//CHUNK
        if offset<value['received']:
            if value['chunks'][index]!=actual:raise APIError(409,'已接收位置的内容不同，不能混合两个文件')
            return _public(value)
        path=session_path(manager,ident)/'payload.db'
        if not path.is_file() or path.stat().st_size<value['received']:raise APIError(409,'服务端临时文件缺失或损坏，请放弃后重新上传')
        with path.open('r+b') as stream:
            stream.truncate(value['received']);stream.seek(offset);stream.write(block)
            stream.flush();os.fsync(stream.fileno())
        value['received']+=len(block);value['chunks'].append(actual)
        _save(manager,value)  # Acknowledgement follows durable data and manifest.
        return _public(value)


def discard(manager, ident):
    with lock(manager,ident):
        value=_load(manager,ident,expired=True)
        if value.get('state')=='validating' or value.get('apply_started'):
            raise APIError(409,'当前上传正在校验或已开始提交，暂不可放弃')
        _remove(manager,ident)
        retained=value['state']=='ready' and value.get('kind','snapshot')=='snapshot'
    try:
        (directory(manager)/(snapshots.identifier(ident)+'.lock')).unlink(missing_ok=True)
    except OSError:
        pass
    return {'discarded':True,'snapshot_retained':retained}


def finalize(manager, ident):
    with lock(manager,ident):
        value=_load(manager,ident)
        if value.get('kind')=='csv':
            from .csv_imports import prepare
            return prepare(manager,value)
        if value.get('kind')=='e':
            from .efile_imports import prepare
            return prepare(manager,value)
        target=snapshots.directory(manager)/(ident+'.db');manifest=target.with_suffix('.json')
        if manifest.is_file():
            item=json.loads(manifest.read_text(encoding='utf-8'))
            if item.get('upload_id')!=ident:raise APIError(409,'快照编号冲突')
            _,result=snapshots.resolve(snapshots.scope(manager,item['database']),ident)
            value.update(state='ready',result=result);_save(manager,value)
            (session_path(manager,ident)/'payload.db').unlink(missing_ok=True)
            return result
        if value['state']=='error':raise APIError(422,value.get('error','上传校验失败，请重新上传'))
        if value['received']!=value['size']:raise APIError(409,'文件尚未上传完整')
        task=current_task();value.update(state='validating',task_id=task['id'] if task else None,task_fingerprint=task['fingerprint'] if task else None)
        _save(manager,value)
        source=session_path(manager,ident)/'payload.db'
        try:
            actual=verify(manager,value)
            document=snapshots._validate(source)
            # 若上传的 SQLite 头部声明为 WAL 模式，发布前转为 DELETE 模式，确保单文件快照可稳定只读访问。
            import sqlite3
            from contextlib import closing
            byte_size=value['size']
            with closing(sqlite3.connect(source, timeout=5)) as conn:
                mode=conn.execute('PRAGMA journal_mode').fetchone()
                if mode and str(mode[0]).lower()=='wal':
                    conn.execute('PRAGMA journal_mode=DELETE')
                    conn.commit()
                    actual=snapshots._hash(source,'计算规范化后校验值')
                    byte_size=source.stat().st_size
            database=manager._database_filename({'filename':document.eng_name+'.db'})
            now=datetime.now(timezone.utc)
            item=dict(id=ident,upload_id=ident,database=database,filename=f'{Path(database).stem}_uploaded_{ident[:8]}.db',
                      created=now.isoformat(),bytes=byte_size,sha256=actual,table_count=len(document.tables),tables=list(document.table_names))
            with committing('发布已校验的上传快照'):
                linked=False
                try:
                    # A crash after link creation but before the manifest leaves
                    # our own hardlink; finish publication without overwriting.
                    if target.exists():
                        if not os.path.samefile(source,target):raise APIError(409,'快照编号已被其他文件使用')
                    else:os.link(source,target)
                    linked=True;atomic_json(manifest,item)
                except BaseException:
                    if linked and not manifest.exists():target.unlink(missing_ok=True)
                    raise
                value.update(state='ready',result=item);_save(manager,value)
                source.unlink(missing_ok=True)
            return item
        except TaskCancelled:
            value.update(state='receiving');_save(manager,value);raise
        except Exception as error:
            # A committed manifest can be recovered even if the session acknowledgement failed.
            if manifest.exists():raise
            value.update(state='error',error=str(error));_save(manager,value)
            if isinstance(error,APIError):raise
            raise APIError(422,'快照校验失败：'+str(error)) from error


def recover_task(manager, task, signature):
    ident=task['path'].split('/')[1]
    value=_load(manager,ident,expired=True)
    if value.get('task_id')!=task['id'] or value.get('task_fingerprint')!=signature:return None
    if value.get('kind') in ('csv','e'):return value.get('result') if value['state']=='ready' else None
    manifest=snapshots.directory(manager)/(ident+'.json')
    if not manifest.is_file():return None
    item=json.loads(manifest.read_text(encoding='utf-8'))
    if item.get('upload_id')!=ident:return None
    resolved=snapshots.resolve(snapshots.scope(manager,item['database']),ident)[1]
    if value.get('state')!='ready':
        try:
            with lock(manager,ident):
                value.update(state='ready',result=resolved)
                _save(manager,value)
                (session_path(manager,ident)/'payload.db').unlink(missing_ok=True)
        except Exception:
            pass
    return resolved


def verify(manager,value):
    digest=hashlib.sha256();done=0
    with (session_path(manager,value['id'])/'payload.db').open('rb') as stream:
        for expected in value['chunks']:
            block=stream.read(CHUNK);done+=len(block);digest.update(block)
            if hashlib.sha256(block).hexdigest()!=expected:raise APIError(422,'服务端分块内容校验失败，请重新上传')
            checkpoint('核对上传内容',done,value['size'])
        if stream.read(1) or done!=value['size']:raise APIError(422,'上传文件长度校验失败')
    actual=digest.hexdigest()
    if value['sha256'] and value['sha256']!=actual:raise APIError(422,'文件SHA-256与输入值不一致')
    return actual
