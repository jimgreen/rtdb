"""Preview-bound maintenance of service artifacts, never business databases."""
from collections import OrderedDict
from contextlib import closing, contextmanager, ExitStack
from functools import wraps
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .errors import APIError
from .sqlite_runtime import file_fence

DEFAULTS = dict(upload_retention_days=1, snapshot_retention_days=30, task_retention_days=7)
LIMIT = 200
PREVIEW_SECONDS = 600
UPLOAD_FILES = frozenset(('state.json', 'payload.sqlite3', 'payload.sqlite3-wal',
    'payload.sqlite3-shm', 'prepared.sqlite3', 'prepared.sqlite3-journal',
    'raw.sqlite3', 'raw.sqlite3-journal'))


def configuration(manager):
    path = manager.data_dir / '.web-storage.json'
    if not path.exists():
        return dict(DEFAULTS)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if set(value) != set(DEFAULTS) or any(type(v) is not int or not 0 <= v <= 36500 for v in value.values()):
            raise ValueError('invalid configuration')
        return value
    except (OSError, ValueError, TypeError) as error:
        raise APIError(409, '存储保留配置无效，请重新保存配置后再清理') from error


@contextmanager
def fence(manager, exclusive=False, timeout=30):
    from core.sqlite_db import SQLiteBusyError
    with ExitStack() as stack:
        try:
            stack.enter_context(file_fence(manager.data_dir / '.web-storage.lock', exclusive, timeout=timeout))
        except SQLiteBusyError as error:
            raise APIError(409, '服务存储正在使用中（上传、下载或快照操作），请稍后重新预览清理') from error
        yield


def activity(function):
    @wraps(function)
    def guarded(manager, *args, **kwargs):
        with fence(manager):
            return function(manager, *args, **kwargs)
    return guarded


def regular(path):
    """Do not traverse symlinks, junctions or other reparse points."""
    info = path.lstat()
    return not path.is_symlink() and not getattr(info, 'st_file_attributes', 0) & 0x400


def inventory(root):
    files = []
    if not root.exists():
        return files
    if not regular(root):
        raise ValueError('目录是链接，已保护')
    for folder, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            if not regular(Path(folder) / name):
                raise ValueError('包含链接，已保护')
        for name in names:
            path = Path(folder) / name
            stat = path.stat()
            files.append([str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns, stat.st_ino])
    return sorted(files)


def upload_expired(manager, value):
    try:
        days = configuration(manager)['upload_retention_days']
    except APIError:
        days = DEFAULTS['upload_retention_days']
    state = value.get('state')
    if not days or state not in ('receiving', 'ready', 'error', 'preview'):
        return False
    ledger = manager.data_dir / '.web-tasks' / 'ledger.sqlite'
    if ledger.exists():
        try:
            with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
                if db.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running') LIMIT 1").fetchone():
                    return False
        except sqlite3.Error:
            return False
    return time.time() - value['updated'] > max(86400, days * 86400)


def size(files):
    return sum(item[1] for item in files)


class StorageMaintenance:
    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.RLock()
        self.previews = OrderedDict()

    def configure(self, payload):
        if not isinstance(payload, dict) or set(payload) != set(DEFAULTS):
            raise APIError(422, '请提供上传、快照和任务详情三项保留天数')
        if any(type(v) is not int or not 0 <= v <= 36500 for v in payload.values()):
            raise APIError(422, '保留天数须为 0 至 36500 的整数，0 表示不清理')
        from .sqlite_service import atomic_json
        with self.lock, fence(self.manager, True):
            atomic_json(self.manager.data_dir / '.web-storage.json', payload)
            self.previews.clear()
        return {'config': dict(payload)}

    def ledger(self):
        return self.manager.data_dir / '.web-tasks' / 'ledger.sqlite'

    def _connect(self, *, readonly=False):
        if not regular(self.ledger().parent) or not regular(self.ledger()):
            raise ValueError('任务账本是链接，已保护')
        mode = 'ro' if readonly else 'rw'
        return sqlite3.connect(self.ledger().as_uri() + f'?mode={mode}', uri=True, timeout=5)

    def _scan(self):
        started = time.perf_counter()
        config = configuration(self.manager)
        now = time.time()
        candidates, protected = [], []
        totals = {kind: dict(bytes=0, files=0, candidates=0, candidate_bytes=0)
                  for kind in ('uploads', 'snapshots', 'tasks', 'logs')}
        def protect(kind, name, reason):
            protected.append(dict(kind=kind, name=name, reason=reason))
        def add(kind, ident, name, updated, files, action):
            item = dict(kind=kind, id=ident, name=name, updated=updated, bytes=size(files), action=action, files=files)
            candidates.append(item)
            totals[kind]['candidates'] += 1
            totals[kind]['candidate_bytes'] += item['bytes']
        def old(stamp, key):
            return config[key] > 0 and stamp < now - config[key] * 86400

        for kind, root in [('uploads', self.manager.data_dir / '.web-uploads'),
                           ('snapshots', self.manager.data_dir / '.web-snapshots'),
                           ('tasks', self.manager.data_dir / '.web-tasks'),
                           ('logs', self.manager.audit_log.log_dir)]:
            try:
                listing = inventory(root)
                totals[kind].update(bytes=size(listing), files=len(listing))
            except (OSError, ValueError) as error:
                protect(kind, root.name, str(error))

        active = 0
        if self.ledger().exists() and regular(self.ledger().parent) and regular(self.ledger()):
            from .tasks import current_task
            task = current_task()
            with closing(self._connect(readonly=True)) as db:
                active = db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running') AND id<>?",
                                    (task['id'] if task else '',)).fetchone()[0]
                total, body_bytes = db.execute('SELECT COUNT(*),COALESCE(SUM(length(CAST(state AS BLOB))+COALESCE(length(CAST(result AS BLOB)),0)),0) FROM tasks').fetchone()
                totals['tasks'].update(records=total, active=active, detail_bytes=body_bytes)
                if config['task_retention_days']:
                    rows = db.execute("SELECT id,updated,length(CAST(state AS BLOB))+COALESCE(length(CAST(result AS BLOB)),0) FROM tasks "
                        "WHERE status IN ('done','failed','cancelled') AND updated<? AND COALESCE(json_extract(state,'$.details_purged'),0)=0 ORDER BY updated LIMIT ?",
                        (now-config['task_retention_days']*86400, LIMIT)).fetchall()
                    for ident, updated, byte_count in rows:
                        add('tasks', ident, ident, updated, [['ledger detail', byte_count, updated, ident]], '清理详情，保留防重记录')
        elif self.ledger().exists():
            active = 1
            protect('tasks', 'ledger.sqlite', '任务账本是链接，无法核实活动任务，保留相关存储')

        root = self.manager.data_dir / '.web-uploads'
        if root.is_dir() and regular(root):
            for folder in sorted(root.iterdir()):
                if not re.fullmatch('[a-f0-9]{32}', folder.name):
                    continue
                try:
                    files = inventory(folder)
                    if not folder.is_dir() or set(item[0] for item in files) - UPLOAD_FILES:
                        raise ValueError('包含未知文件，保留会话')
                    value = json.loads((folder / 'state.json').read_text(encoding='utf-8'))
                    if value['id'] != folder.name:
                        raise ValueError('会话身份不一致')
                    if value['state'] not in ('receiving', 'ready', 'error', 'preview'):
                        raise ValueError('上传正在校验或导入，保留会话')
                    if active:
                        raise ValueError('有活动任务，暂不清理上传暂存')
                    if old(max(value['updated'], max((x[2]/1e9 for x in files), default=0)), 'upload_retention_days'):
                        add('uploads', folder.name, value['filename'], value['updated'], files, '删除上传暂存（已发布快照保留）')
                except (OSError, ValueError, KeyError, TypeError) as error:
                    protect('uploads', folder.name, str(error))

        root = self.manager.data_dir / '.web-snapshots'
        if root.is_dir() and regular(root):
            for path in sorted(root.iterdir()):
                if path.name.endswith('.restore.json'):
                    protect('snapshots', path.name, '恢复回执保留，支持中断恢复')
                    continue
                try:
                    if not regular(path):
                        raise ValueError('链接文件保留')
                    if path.is_dir():
                        # Old temporary folders have no durable owner marker. Do not guess.
                        raise ValueError('临时目录归属无法确认，保留')
                    if not re.fullmatch('[a-f0-9]{32}\\.sqlite3', path.name):
                        if path.suffix == '.json' and not path.with_suffix('.sqlite3').exists():
                            raise ValueError('缺少配对快照，保留清单')
                        continue
                    manifest = path.with_suffix('.json')
                    if not manifest.is_file() or not regular(manifest):
                        raise ValueError('缺少有效清单，保留孤立快照')
                    item = json.loads(manifest.read_text(encoding='utf-8'))
                    if item['id'] != path.stem or item['bytes'] != path.stat().st_size:
                        raise ValueError('快照身份或大小与清单不符，保留')
                    if active:
                        raise ValueError('有活动任务，暂不清理快照')
                    files = [[p.name, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ino] for p in (path, manifest)]
                    updated = max(x[2]/1e9 for x in files)
                    if old(updated, 'snapshot_retention_days'):
                        add('snapshots', path.stem, item['filename'], updated, files, '删除快照及配对清单')
                except (OSError, ValueError, KeyError, TypeError) as error:
                    protect('snapshots', path.name, str(error))
        totals['logs']['retention_days'] = self.manager.audit_log.config()['retention_days']
        # 按类别公平分配预览配额，避免大量过期任务饿死 uploads / snapshots。
        buckets = {kind: [item for item in candidates if item['kind'] == kind] for kind in ('tasks', 'uploads', 'snapshots')}
        selected = []
        remaining = LIMIT
        active_kinds = [kind for kind in ('tasks', 'uploads', 'snapshots') if buckets[kind]]
        offsets = {kind: 0 for kind in active_kinds}
        while remaining > 0 and active_kinds:
            share = max(1, remaining // len(active_kinds))
            next_kinds = []
            for kind in active_kinds:
                if remaining <= 0:
                    break
                take = min(share, remaining, len(buckets[kind]) - offsets[kind])
                if take > 0:
                    selected.extend(buckets[kind][offsets[kind]:offsets[kind] + take])
                    offsets[kind] += take
                    remaining -= take
                if offsets[kind] < len(buckets[kind]):
                    next_kinds.append(kind)
            active_kinds = next_kinds
        return dict(config=config, statistics=totals, candidates=selected,
                    candidate_count=len(candidates), limited=len(candidates) > len(selected),
                    protected=protected[:LIMIT], protected_count=len(protected),
                    scan_ms=round((time.perf_counter()-started)*1000, 3))

    def preview(self):
        with self.lock, fence(self.manager):
            result = self._scan()
            token = uuid.uuid4().hex
            self.previews[token] = (time.monotonic(), result)
            while len(self.previews) > 16:
                self.previews.popitem(last=False)
            return {**result, 'token': token, 'expires_in': PREVIEW_SECONDS}

    def clean(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('token'), str):
            raise APIError(422, '请先预览可清理内容')
        with self.lock, fence(self.manager, True, timeout=.1):
            saved = self.previews.pop(payload['token'], None)
            if not saved or time.monotonic()-saved[0] > PREVIEW_SECONDS:
                raise APIError(409, '清理预览已过期或已使用，请重新预览')
            previous = saved[1]
            fresh = self._scan()
            current = {(i['kind'], i['id']): i for i in fresh['candidates']}
            if previous['config'] != fresh['config'] or any(current.get((i['kind'], i['id'])) != i for i in previous['candidates']):
                raise APIError(409, '候选文件或任务状态已变化，未清理，请重新预览')
            result = dict(ok=True, cleaned=0, deleted_file_bytes=0, compacted_tasks=0, failures=[])
            tasks = [item for item in previous['candidates'] if item['kind'] == 'tasks']
            if tasks:
                try:
                    self._compact_tasks(tasks)
                    result.update(cleaned=len(tasks), compacted_tasks=len(tasks))
                except (OSError, ValueError, sqlite3.Error) as error:
                    result['ok'] = False
                    result['failures'].append(dict(kind='tasks', name='任务详情批次', error=str(error)))
            from .database_uploads import _remove
            for item in previous['candidates']:
                if item['kind'] == 'tasks':
                    continue
                try:
                    if item['kind'] == 'uploads':
                        _remove(self.manager, item['id'])
                    elif item['kind'] == 'snapshots':
                        root = self.manager.data_dir / '.web-snapshots'
                        # Delete data first: if Windows denies an open file, the pair survives.
                        for name, *_ in item['files']:
                            (root / name).unlink()
                    result['cleaned'] += 1
                    if item['kind'] != 'tasks':
                        result['deleted_file_bytes'] += item['bytes']
                except (OSError, ValueError, sqlite3.Error) as error:
                    result['ok'] = False
                    result['failures'].append(dict(kind=item['kind'], name=item['name'], error=str(error)))
            return result

    def _compact_tasks(self, items):
        # One bounded transaction avoids a connection and durable commit per row.
        with closing(self._connect()) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            for item in items:
                row = db.execute("SELECT state FROM tasks WHERE id=? AND updated=? AND status IN ('done','failed','cancelled')",
                                 (item['id'], item['updated'])).fetchone()
                if not row:
                    raise ValueError('任务已变化，本批任务详情未清理')
                state = json.loads(row[0])
                kept = {k: v for k, v in state.items() if k in ('id','database','method','path','status','created','phase','error','error_status','revision')}
                kept.update(details_purged=True, cancellable=False, completed_tables=[])
                for key, limit in [('phase',256),('error',4096)]:
                    if isinstance(kept.get(key),str): kept[key] = kept[key][:limit]
                db.execute('UPDATE tasks SET state=?,result=NULL WHERE id=?', (json.dumps(kept, ensure_ascii=False), item['id']))
