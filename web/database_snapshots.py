"""Consistent SQLite snapshots, bounded I/O and restore-as-new publication."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from core.sqlite_schema import load_embedded_database, ensure_embedded_schema
from core.definition_files import python_definition
from .errors import APIError
from .sqlite_service import atomic_json, atomic_text, fingerprint
from .tasks import checkpoint, committing, current_task, cancellation_requested, TaskCancelled
from .storage_maintenance import activity

CHUNK = 1024 * 1024
MAX_SECONDS = 300


def directory(manager):
    path = manager.data_dir / '.web-snapshots'
    path.mkdir(exist_ok=True)
    return path


def scope(manager, filename):
    """Backup access does not require the original database to still exist."""
    if not filename: return manager
    import copy
    scoped = copy.copy(manager)
    scoped.database_file = manager._database_filename({'filename': filename})
    from .sqlite_service import is_native_database
    scoped.read_only = is_native_database(manager.data_dir / scoped.database_file)
    scoped._journal = manager.schema_dir / ('.sqlite-web.' + Path(scoped.database_file).stem + '.pending.json')
    return scoped


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise APIError(400, '无效的快照编号')
    return value


def _public(item):
    return {key: value for key, value in item.items() if key != 'task_fingerprint'}


def resolve(manager, ident):
    if not manager.database_file: raise APIError(400, '请指定快照所属数据库')
    root = directory(manager); ident = identifier(ident)
    path = root / (ident + '.sqlite3'); manifest = root / (ident + '.json')
    if not path.is_file() or not manifest.is_file() or path.resolve().parent != root.resolve():
        raise APIError(404, '快照不存在或尚未完成')
    item = json.loads(manifest.read_text(encoding='utf-8'))
    if item.get('database') != manager.database_file or item.get('id') != ident:
        raise APIError(404, '当前数据库中没有此快照')
    if path.stat().st_size != item['bytes']:
        raise APIError(409, '快照文件大小已变化，拒绝使用')
    return path, _public(item)


def listing(manager):
    items = []
    for path in directory(manager).glob('*.json'):
        if not re.fullmatch('[a-f0-9]{32}', path.stem): continue
        try:
            meta = json.loads(path.read_text(encoding='utf-8'))
            _, item = resolve(manager if manager.database_file else scope(manager, meta['database']), path.stem)
            items.append(item)
        except (APIError, OSError, ValueError, KeyError): continue
    items.sort(key=lambda item: item['created'], reverse=True)
    return {'snapshots': items[:50], 'total': len(items)}


def _hash(path, phase='计算快照校验值'):
    digest = hashlib.sha256(); size = path.stat().st_size; done = 0
    with path.open('rb') as stream:
        while block := stream.read(CHUNK):
            digest.update(block); done += len(block); checkpoint(phase, done, size)
    return digest.hexdigest()


def _validate(path):
    checkpoint('检查数据库完整性')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.set_progress_handler(lambda: int(cancellation_requested()), 10000)
        try:
            errors = [row[0] for row in connection.execute('PRAGMA quick_check') if row[0] != 'ok']
            if errors: raise APIError(422, '数据库完整性检查失败：' + '; '.join(errors[:5]))
        except sqlite3.OperationalError:
            if cancellation_requested(): raise TaskCancelled()
            raise
    document = load_embedded_database(path)
    if document is None or not document.is_grouped:
        raise APIError(422, '整库快照要求有效的多表内置定义')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        for definition in document.tables:
            checkpoint('核对表结构：' + definition.eng_name)
            quote = lambda s: '"' + s.replace('"', '""') + '"'
            metadata = connection.execute('SELECT fingerprint,row_count FROM ' + quote('rtdb_metadata_' + definition.eng_name) + ' WHERE version=1').fetchone()
            if not metadata or metadata[0] != fingerprint(definition.to_mapping()):
                raise APIError(409, '内置定义与物理表不一致：' + definition.eng_name)
            count = connection.execute('SELECT COUNT(*) FROM ' + quote('rtdb_data_' + definition.eng_name)).fetchone()[0]
            if count != metadata[1]: raise APIError(409, '记录数元数据不一致：' + definition.eng_name)
    return document


@activity
def create(manager):
    if not manager.database_file: raise APIError(400, '请先选择数据库')
    root = directory(manager); source = manager.data_dir / manager.database_file
    task = current_task(); ident = task['id'] if task else uuid.uuid4().hex
    target = root / (ident + '.sqlite3'); manifest = root / (ident + '.json')
    if manifest.exists(): return resolve(manager, ident)[1]
    started = time.perf_counter(); published = False
    # Each invocation owns only its uniquely created temporary directory.
    with tempfile.TemporaryDirectory(prefix='.building-', dir=root) as temporary:
        stage = Path(temporary) / 'snapshot.sqlite3'
        with manager.read_scope():
            if not source.is_file(): raise APIError(404, '数据库文件不存在')
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as src, closing(sqlite3.connect(stage)) as dst:
                src.execute('BEGIN')
                src.execute('SELECT count(*) FROM sqlite_master').fetchone()  # Pin one WAL read snapshot.
                def progress(status, remaining, total):
                    checkpoint('正在生成整库快照', total - remaining, total)
                    if time.perf_counter() - started > MAX_SECONDS:
                        raise APIError(408, '生成快照超过 5 分钟，请稍后重试')
                src.backup(dst, pages=256, progress=progress, sleep=.05)
                src.rollback()
                dst.execute('PRAGMA journal_mode=DELETE')
        document = _validate(stage)
        digest = _hash(stage)
        now = datetime.now(timezone.utc)
        item = dict(id=ident, database=manager.database_file, created=now.isoformat(),
                    filename=f'{source.stem}_{now:%Y%m%d_%H%M%S}_{ident[:8]}.sqlite3',
                    bytes=stage.stat().st_size, sha256=digest, table_count=len(document.tables),
                    tables=list(document.table_names), task_fingerprint=task['fingerprint'] if task else None,
                    duration_ms=round((time.perf_counter()-started)*1000, 3))
        try:
            with committing('发布已校验快照'):
                os.link(stage, target)  # Atomic, fails if a target already exists; never replaces it.
                published = True
                atomic_json(manifest, item)
        except BaseException:
            if published and not manifest.exists(): target.unlink(missing_ok=True)
            raise
    return _public(item)


def _origin(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        try: row = connection.execute('SELECT result FROM rtdb_web_snapshot_origin WHERE version=1').fetchone()
        except sqlite3.OperationalError: return None
        return json.loads(row[0]) if row else None


def recover_definition(manager, pending):
    """Complete sidecar exports after publication; an unpublished intent is harmless."""
    name = manager.name(pending['name']); path = manager.data_dir / (name + '.sqlite3')
    if path.is_file():
        origin = _origin(path)
        if not origin or origin.get('restore_id') != pending['restore_id']:
            raise APIError(409, '快照恢复目标身份不匹配，未覆盖任何文件')
        document = load_embedded_database(path).to_mapping()
        atomic_json(manager.schema_dir / (name + '.json'), document)
        atomic_text(manager.schema_dir / (name + '.py'), python_definition(document))
    manager._journal.unlink(missing_ok=True)


@activity
def restore(manager, ident, payload):
    source, item = resolve(manager, ident)
    name = manager.name(payload.get('name', ''))
    label = payload.get('chn_name') or name
    if not isinstance(label, str): raise APIError(400, '中文名称必须为文字')
    label = label.strip() or name
    target = manager.data_dir / (name + '.sqlite3')
    outputs = [target, manager.schema_dir / (name + '.json'), manager.schema_dir / (name + '.py'),
               target.with_name(target.name + '-wal'), target.with_name(target.name + '-shm')]
    if any(path.exists() for path in outputs): raise APIError(409, f'目标数据库或定义已存在：{name}')
    task = current_task(); restore_id = task['id'] if task else uuid.uuid4().hex
    result = dict(restored=True, filename=target.name, name=name, snapshot_id=ident,
                  source_database=manager.database_file, restore_id=restore_id, tables=item['tables'])
    with tempfile.TemporaryDirectory(prefix='.snapshot-restore-', dir=manager.data_dir) as temporary:
        stage = Path(temporary) / target.name
        digest = hashlib.sha256(); done = 0
        with source.open('rb') as src, stage.open('xb') as dst:
            while block := src.read(CHUNK):
                dst.write(block); digest.update(block); done += len(block)
                checkpoint('校验并恢复快照', done, item['bytes'])
            dst.flush(); os.fsync(dst.fileno())
        if digest.hexdigest() != item['sha256']: raise APIError(409, '快照 SHA-256 校验失败，未创建数据库')
        _validate(stage)
        with closing(sqlite3.connect(stage)) as connection, connection:
            connection.execute('PRAGMA synchronous=FULL')
            ensure_embedded_schema(connection, name, label)
            connection.execute('CREATE TABLE IF NOT EXISTS rtdb_web_snapshot_origin(version INTEGER PRIMARY KEY CHECK(version=1), result TEXT NOT NULL)')
            connection.execute('INSERT OR REPLACE INTO rtdb_web_snapshot_origin VALUES(1,?)', (json.dumps(result, ensure_ascii=False),))
        checkpoint('准备发布恢复数据库')
        # Brief catalogue exclusion only at publication. Staging/copying never holds it.
        with manager._runtime.scope(None, exclusive=True), committing('发布恢复数据库'):
            if any(path.exists() for path in outputs): raise APIError(409, f'目标数据库或定义已存在：{name}')
            import copy
            scoped = copy.copy(manager); scoped.database_file = target.name
            scoped._journal = manager.schema_dir / ('.sqlite-web.' + name + '.pending.json')
            if scoped._journal.exists(): raise APIError(409, '目标数据库有待恢复的操作，请先检查')
            intent = dict(kind='snapshot-restore', name=name, restore_id=restore_id)
            atomic_json(scoped._journal, intent)
            atomic_json(directory(manager) / (restore_id + '.restore.json'),
                        dict(database=manager.database_file, task_fingerprint=task['fingerprint'] if task else None, result=result))
            try: os.link(stage, target)
            except BaseException:
                scoped._journal.unlink(missing_ok=True)
                raise
            try: recover_definition(scoped, intent)
            except Exception as error:
                result['definition_warning'] = f'数据库已恢复，JSON/Python 导出待重试：{error}'
    return result


def recover_task(manager, task, signature):
    """Recover completed file publications when the process stopped before task ledger update."""
    root = directory(manager); ident = identifier(task['id'])
    if task['path'] == 'database-snapshots':
        manifest = root / (ident + '.json')
        if not manifest.is_file(): return None
        item = json.loads(manifest.read_text(encoding='utf-8'))
        if item.get('task_fingerprint') != signature: return None
        return resolve(manager, ident)[1]
    manifest = root / (ident + '.restore.json')
    if not manifest.is_file(): return None
    item = json.loads(manifest.read_text(encoding='utf-8'))
    if item.get('database') != manager.database_file or item.get('task_fingerprint') != signature: return None
    result = item['result']; name = manager.name(result['name']); target = manager.data_dir / (name + '.sqlite3')
    if target.is_file() and _origin(target) == result: return result
    return None


def byte_range(header, size):
    if not header: return 0, size - 1, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()): raise APIError(416, '不支持的下载范围')
    left, right = match.groups()
    if not left:
        count = int(right)
        if not count: raise APIError(416, '无效的下载范围')
        start, end = max(0, size - count), size - 1
    else: start, end = int(left), min(int(right), size - 1) if right else size - 1
    if start >= size or end < start: raise APIError(416, '下载范围超出文件大小')
    return start, end, True
