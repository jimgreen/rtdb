"""Database lifetime fences and bounded conditional-query metadata caches."""
from collections import OrderedDict
from contextlib import contextmanager
import copy
import os
from pathlib import Path
import sqlite3
import threading
import time

from core.file_mapped_db import _ReadWriteLock
from core.sqlite_db import SQLiteBusyError


@contextmanager
def file_fence(path, exclusive, timeout=30):
    """Shared readers / exclusive schema changes, also across Web processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b', buffering=0) as stream:
        deadline = time.monotonic() + timeout
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Overlapped(ctypes.Structure):
                _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
                          ('Offset',wintypes.DWORD),('OffsetHigh',wintypes.DWORD),('hEvent',wintypes.HANDLE)]
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            lock=kernel.LockFileEx; unlock=kernel.UnlockFileEx
            lock.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
            unlock.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
            lock.restype=unlock.restype=wintypes.BOOL
            handle=msvcrt.get_osfhandle(stream.fileno()); overlapped=Overlapped()
            while not lock(handle,1 | (2 if exclusive else 0),0,1,0,ctypes.byref(overlapped)):
                error=ctypes.get_last_error()
                if error not in (33,158): raise ctypes.WinError(error)
                if time.monotonic() >= deadline: raise SQLiteBusyError('等待数据库结构锁超时')
                time.sleep(.005)
            try: yield
            finally:
                if not unlock(handle,0,1,0,ctypes.byref(overlapped)): raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise SQLiteBusyError('等待数据库结构锁超时')
                    time.sleep(.005)
            try: yield
            finally: fcntl.flock(stream,fcntl.LOCK_UN)


class QueryVersions:
    """One read-only observer per file; data_version compared on that connection only."""
    def __init__(self, directory):
        self.directory=directory; self.lock=threading.RLock(); self.observers=OrderedDict(); self.generation=0
        self.anchors=OrderedDict()

    def page_anchor(self, token, signature, stamp, offset):
        with self.lock:
            value=self.anchors.get(token) if isinstance(token,str) else None
            if value and value['signature']==signature and value['stamp']==stamp and value['offset']==offset and time.monotonic()-value['created']<900:
                return value
            return None

    def page_token(self, signature, stamp, offset, anchor, backwards):
        import uuid
        with self.lock:
            token=uuid.uuid4().hex
            self.anchors[token]=dict(signature=signature,stamp=stamp,offset=offset,anchor=anchor,backwards=backwards,created=time.monotonic())
            while len(self.anchors)>512:self.anchors.popitem(last=False)
            return token

    def _observe(self, filename):
        path=self.directory / filename
        stat=path.stat(); identity=(stat.st_dev,stat.st_ino)
        entry=self.observers.get(filename)
        if entry and entry['identity'] != identity:
            entry['connection'].close(); del self.observers[filename]; entry=None
        if entry is None:
            connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,check_same_thread=False,isolation_level=None)
            self.generation += 1
            entry={'connection':connection,'identity':identity,'queries':OrderedDict(),'generation':self.generation}
            self.observers[filename]=entry
        self.observers.move_to_end(filename)
        while len(self.observers)>16:
            _, old=self.observers.popitem(last=False); old['connection'].close()
        version=entry['connection'].execute('PRAGMA data_version').fetchone()[0]
        return entry, (entry['generation'],identity,version)

    def probe(self, filename, key, version):
        with self.lock:
            entry, stamp=self._observe(filename)
            saved=entry['queries'].get(key)
            if saved and saved[0] == stamp and saved[1]['result_version'] == version:
                return copy.deepcopy(saved[1]), stamp
            return None, stamp

    def store(self, filename, key, stamp, result):
        with self.lock:
            entry, now=self._observe(filename)
            if now != stamp: return  # A commit raced the query; do not cache that snapshot.
            metadata={k:v for k,v in result.items() if k not in ('rows','records','record_order','delta')}
            metadata['unchanged']=True
            entry['queries'][key]=(stamp,copy.deepcopy(metadata)); entry['queries'].move_to_end(key)
            while len(entry['queries'])>32: entry['queries'].popitem(last=False)

    def close(self, filename=None):
        with self.lock:
            for name in list(self.observers):
                if filename is None or filename == name: self.observers.pop(name)['connection'].close()


class SQLiteRuntime:
    def __init__(self, directory, schema_dir):
        self.schema_dir=schema_dir; self.versions=QueryVersions(directory)
        self.guard=threading.Lock(); self.locks={}; self.local=threading.local()

    @contextmanager
    def _hold(self, path, exclusive):
        key=os.path.normcase(str(path.resolve()))
        held=getattr(self.local,'held',None)
        if held is None: held=self.local.held={}
        if key in held:
            if exclusive and not held[key]: raise RuntimeError('cannot upgrade a database lifetime read lock')
            yield; return
        with self.guard: guard=self.locks.setdefault(key,_ReadWriteLock())
        with guard if exclusive else guard.read():
            with file_fence(path,exclusive):
                held[key]=exclusive
                try: yield
                finally: del held[key]

    @contextmanager
    def scope(self, filename=None, *, exclusive=False):
        # Catalogue/file replacement excludes all users. Table DDL excludes only its database.
        with self._hold(self.schema_dir / '.sqlite-web.lock', exclusive and filename is None):
            if filename:
                with self._hold(self.schema_dir / ('.sqlite-web.'+Path(filename).stem+'.lock'),exclusive):
                    if exclusive: self.versions.close(filename)
                    yield
            else:
                if exclusive: self.versions.close()
                yield

    def close(self):
        self.versions.close()
