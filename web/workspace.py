"""Per-directory Web contexts for databases below a configured workspace root."""
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import threading
from types import SimpleNamespace

from .errors import APIError
from .management import SQLiteManagement
from .sqlite_service import SQLiteManager, _DATABASE_FILE_RE
from .tasks import TaskRegistry
from .write_receipts import AuditDelivery


class WorkspaceCatalog:
    def __init__(self, root, default):
        self.root = Path(root).resolve()
        self.default = default
        self.contexts = {}
        self.lock = threading.RLock()

    def directory(self, relative):
        if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative
                or PureWindowsPath(relative).drive or PurePosixPath(relative).is_absolute()
                or (relative != '.' and any(part in ('', '.', '..') or part.startswith('.')
                                            for part in relative.split('/')))):
            raise APIError(400, '工作目录必须是 workspace 内的相对路径，使用 / 分隔')
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise APIError(400, '工作目录不能超出 workspace')
        if not path.is_dir():
            raise APIError(404, '工作目录不存在')
        return path

    def listing(self):
        directories = []
        if self.root.is_dir():
            for base, children, files in os.walk(self.root, followlinks=False):
                folder = Path(base)
                children[:] = sorted(name for name in children
                    if not name.startswith('.') and name not in ('node_modules', '__pycache__', 'rtdb-operation-logs')
                    and not (folder / name).is_symlink()
                    and not getattr(folder / name, 'is_junction', lambda: False)())
                names = sorted(name for name in files if _DATABASE_FILE_RE.fullmatch(name)
                               and (folder / name).resolve().is_relative_to(self.root))
                directories.append({'path': folder.relative_to(self.root).as_posix(),
                                    'files': names, 'database_count': len(names)})
        return {'workspace_root': str(self.root), 'directories': directories,
                'default_data_dir': str(self.default.sqlite_manager.data_dir)}

    def get(self, relative):
        if not relative:
            return self.default
        path = self.directory(relative)
        if path == self.default.sqlite_manager.data_dir:
            return self.default
        with self.lock:
            if path not in self.contexts:
                if len(self.contexts) >= 64:
                    raise APIError(503, '已打开64个工作目录，请重启服务后继续')
                manager = SQLiteManager(path, path / '.rtdb-schema')
                tasks = None
                try:
                    tasks = TaskRegistry(path / '.web-tasks', manager)
                    delivery = manager.audit_delivery = AuditDelivery(manager)
                except BaseException:
                    if tasks is not None:
                        tasks.close()
                    manager.close()
                    raise
                self.contexts[path] = SimpleNamespace(sqlite_manager=manager,
                    management=SQLiteManagement(manager), tasks=tasks, audit_delivery=delivery)
            return self.contexts[path]

    def close(self):
        with self.lock:
            for context in self.contexts.values():
                context.tasks.close()
                context.audit_delivery.close()
                context.sqlite_manager.close()
            self.contexts.clear()
