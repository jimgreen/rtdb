"""Per-directory Web contexts for databases below a configured workspace root."""
from collections import OrderedDict
import errno
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import threading
import time
from types import SimpleNamespace

from .errors import APIError
from .management import SQLiteManagement
from .sqlite_service import SQLiteManager, is_listed_database
from .tasks import TaskRegistry
from .write_receipts import AuditDelivery


_RUNTIME_DIRECTORIES = {
    '.web-archive', '.web-snapshots', '.web-tasks', '.web-uploads', 'rtdb-operation-logs',
}
_RUNTIME_FILES = {
    '.web-storage.json', '.web-storage.lock', 'rtdb-operation-log.jsonl',
    'rtdb-operation-log-config.json', 'rtdb-operation-log-config.json.lock',
}
_UNLISTED_DIRECTORIES = {'node_modules', '__pycache__', 'rtdb-operation-logs'}
_SQLITE_SUFFIXES = ('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.sqlite3-journal',
                    '.sqlite', '.sqlite-wal', '.sqlite-shm', '.sqlite-journal',
                    '.db', '.db-wal', '.db-shm', '.db-journal')


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _ordinary(path):
    try:
        return not _is_link(path.lstat())
    except FileNotFoundError:
        return False


class WorkspaceCatalog:
    def __init__(self, root, default=None, *, strict_root=False):
        self.root = Path(root).resolve()
        self.strict_root = strict_root
        self.root_id = sha256(os.fsencode(os.path.normcase(self.root))).hexdigest()
        self.default = default
        self.contexts = OrderedDict()
        self.lock = threading.RLock()
        self._storage_cache = {}
        self._storage_cache_ttl = 2.0

    def _check_path(self, path):
        if not path.is_relative_to(self.root):
            raise APIError(400, 'Web 文件路径不能超出根目录')
        component = self.root
        for part in ('', *path.relative_to(self.root).parts):
            component /= part
            try:
                linked = _is_link(component.lstat())
            except FileNotFoundError:
                linked = False  # New managed files/directories may not exist yet.
            if linked:
                raise APIError(400, 'Web 文件路径不能使用符号链接或联接点')

    def _check_entries(self, directory, *, select=None, recursive=False):
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            return
        with entries:
            for entry in entries:
                if select is not None and not select(entry.name.lower()):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # A different request may remove SQLite sidecars or temporary files.
                if _is_link(info):
                    raise APIError(400, 'Web 数据目录包含符号链接或联接点')
                if stat.S_ISDIR(info.st_mode) and (recursive or entry.name.lower() in _RUNTIME_DIRECTORIES):
                    self._check_entries(entry.path, recursive=True)

    @staticmethod
    def _dir_stamp(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if _is_link(info):
            raise APIError(400, 'Web 文件路径不能使用符号链接或联接点')
        return (info.st_mtime_ns, info.st_size)

    def validate_storage(self, data_dir, schema_dir):
        if not self.strict_root:
            return
        cache_key = (data_dir, schema_dir)
        now = time.monotonic()
        stamp = (self._dir_stamp(data_dir), self._dir_stamp(schema_dir))
        with self.lock:
            cached = self._storage_cache.get(cache_key)
            if cached is not None and cached[1] == stamp and (now - cached[0]) < self._storage_cache_ttl:
                return
        self._check_path(data_dir)
        self._check_path(schema_dir)
        self._check_entries(data_dir, select=lambda name: name.endswith(_SQLITE_SUFFIXES)
                            or name in _RUNTIME_FILES or name in _RUNTIME_DIRECTORIES)
        self._check_entries(schema_dir)
        with self.lock:
            self._storage_cache[cache_key] = (now, stamp)

    def storage_paths(self, data_dir, schema_dir):
        """Resolve server configuration before constructing anything that writes files."""
        def configured(value, fallback):
            return Path(os.path.abspath(self.root / value)) if value is not None else fallback

        data = configured(data_dir, self.root)
        schema = configured(schema_dir, data / '.rtdb-schema')
        if not self.root.is_dir():
            raise ValueError('配置的 Web 根目录不存在或不是目录')
        try:
            self.validate_storage(data, schema)
        except APIError as error:
            raise ValueError(error.message) from error
        return data, schema

    def directory(self, relative, *, reject_links=False):
        if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative
                or PureWindowsPath(relative).drive or PurePosixPath(relative).is_absolute()
                or (relative != '.' and any(part in ('', '.', '..') or part.startswith('.')
                                            for part in relative.split('/')))):
            raise APIError(400, '工作目录必须是 workspace 内的相对路径，使用 / 分隔')
        candidate = self.root / relative
        if self.strict_root or reject_links:
            self._check_path(candidate)
        path = candidate.resolve()
        if not path.is_relative_to(self.root):
            raise APIError(400, '工作目录不能超出 workspace')
        if not path.is_dir():
            raise APIError(404, '工作目录不存在')
        return path

    def create_directory(self, relative, name):
        if (not isinstance(name, str) or not name or name != name.strip()
                or name.startswith('.') or name.endswith('.') or not name.isprintable()
                or any(character in '<>:"/\\|?*' for character in name)
                or PureWindowsPath(name).is_reserved()
                or name.lower() in _UNLISTED_DIRECTORIES or name.lower() in _RUNTIME_FILES):
            raise APIError(400, '请输入有效的单级目录名')
        parent = self.directory(relative, reject_links=True)
        target = parent / name
        try:
            target.mkdir()
        except FileExistsError as error:
            raise APIError(409, '同名文件或目录已存在') from error
        except (FileNotFoundError, NotADirectoryError) as error:
            raise APIError(404, '父目录不存在') from error
        except PermissionError as error:
            raise APIError(403, '没有在此目录下创建文件夹的权限') from error
        except OSError as error:
            if error.errno in (errno.EINVAL, errno.ENAMETOOLONG) or getattr(error, 'winerror', None) == 206:
                raise APIError(400, '目录名称或路径无效') from error
            raise
        return {'path': target.relative_to(self.root).as_posix()}

    def listing(self):
        directories = []
        if self.strict_root:
            self._check_path(self.root)
        if self.root.is_dir():
            for base, children, files in os.walk(self.root, followlinks=False):
                folder = Path(base)
                children[:] = sorted(name for name in children
                    if not name.startswith('.') and name.lower() not in _UNLISTED_DIRECTORIES
                    and _ordinary(folder / name))
                names = sorted(
                    name for name in files
                    if name.lower().endswith(('.sqlite3', '.sqlite', '.db'))
                    and _ordinary(folder / name)
                    and is_listed_database(folder / name)
                )
                directories.append({'path': folder.relative_to(self.root).as_posix(),
                                    'files': names, 'database_count': len(names)})
        return {'workspace_root': str(self.root), 'directories': directories,
                'default_data_dir': str(self.default.sqlite_manager.data_dir)}

    @staticmethod
    def _is_context_idle(context):
        if getattr(context, 'in_flight', 0) > 0:
            return False
        with context.tasks.lock:
            if context.tasks.running_databases or context.tasks.queues:
                return False
        with context.audit_delivery.condition:
            if (context.audit_delivery.pending or context.audit_delivery.active
                    or context.audit_delivery.archive_queue or context.audit_delivery.archive_active):
                return False
        return True

    @staticmethod
    def _close_context(context):
        context.tasks.close()
        context.audit_delivery.close()
        context.sqlite_manager.close()

    def get(self, relative, *, _acquire=False):
        default_manager = self.default.sqlite_manager
        path = self.directory(relative) if relative else default_manager.data_dir
        if path == default_manager.data_dir:
            self.validate_storage(path, default_manager.schema_dir)
            if _acquire:
                with self.lock:
                    self.default.in_flight = getattr(self.default, 'in_flight', 0) + 1
            return self.default
        schema = path / '.rtdb-schema'
        self.validate_storage(path, schema)
        evicted = None
        with self.lock:
            if path in self.contexts:
                self.contexts.move_to_end(path)
                context = self.contexts[path]
                if _acquire:
                    context.in_flight = getattr(context, 'in_flight', 0) + 1
                return context
            if len(self.contexts) >= 64:
                victim_key = next(
                    (key for key, ctx in self.contexts.items() if self._is_context_idle(ctx)),
                    None,
                )
                if victim_key is None:
                    raise APIError(503, '已打开64个活跃工作目录，请等待后台任务完成或重启服务后继续')
                evicted = self.contexts.pop(victim_key)
            manager = SQLiteManager(path, schema, strict_paths=self.strict_root)
            tasks = None
            try:
                tasks = TaskRegistry(path / '.web-tasks', manager)
                delivery = manager.audit_delivery = AuditDelivery(manager)
            except BaseException:
                if tasks is not None:
                    tasks.close()
                manager.close()
                raise
            context = SimpleNamespace(sqlite_manager=manager,
                management=SQLiteManagement(manager), tasks=tasks, audit_delivery=delivery,
                in_flight=1 if _acquire else 0)
            self.contexts[path] = context
        if evicted is not None:
            try:
                self._close_context(evicted)
            except Exception:
                pass
        return context

    def acquire(self, relative):
        """获取工作区上下文并增加活跃请求引用计数，防止并发驱逐关闭。"""
        return self.get(relative, _acquire=True)

    def release(self, context):
        """释放活跃请求引用计数。"""
        if context is None:
            return
        with self.lock:
            current = getattr(context, 'in_flight', 0)
            context.in_flight = current - 1 if current > 0 else 0

    def close(self):
        with self.lock:
            for context in self.contexts.values():
                context.tasks.close()
                context.audit_delivery.close()
                context.sqlite_manager.close()
            self.contexts.clear()
