"""Rebuildable SQLite search index; date-partitioned JSONL remains authoritative."""
from contextlib import closing
import json
import os
import sqlite3
import time

from core.file_mapped_db import _locked
from .tasks import checkpoint as task_checkpoint


class AuditIndex:
    def __init__(self, audit):
        self.audit = audit
        self.path = audit.log_dir / '.query-index.db'
        self.lock_path = audit.log_dir / '.query-index.lock'

    @staticmethod
    def _parse(raw):
        try:
            value = json.loads(raw.decode('utf-8', errors='replace'))
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None

    _SCHEMA_VERSION = '2'

    @staticmethod
    def _register_functions(connection):
        connection.create_function('rtdb_casefold', 1, lambda s: s.casefold() if isinstance(s, str) else '')

    @classmethod
    def _schema(cls, connection):
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        ''')
        ver = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        if ver != (cls._SCHEMA_VERSION,):
            connection.executescript('''
                DROP TABLE IF EXISTS entries;
                DROP TABLE IF EXISTS sources;
            ''')
            connection.execute("INSERT OR REPLACE INTO settings VALUES ('schema_version',?)", (cls._SCHEMA_VERSION,))
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS sources (
                source TEXT PRIMARY KEY, identity TEXT, size INTEGER, mtime INTEGER,
                offset INTEGER, tail BLOB);
            CREATE TABLE IF NOT EXISTS entries (
                source TEXT NOT NULL REFERENCES sources(source) ON DELETE CASCADE,
                position INTEGER NOT NULL, record_date TEXT, category TEXT, status TEXT,
                table_name TEXT, operation TEXT, payload TEXT,
                PRIMARY KEY(source, position));
            CREATE INDEX IF NOT EXISTS entries_date ON entries(record_date, source, position);
            CREATE INDEX IF NOT EXISTS entries_date_status ON entries(record_date, status, source, position);
            CREATE INDEX IF NOT EXISTS entries_status ON entries(status, category, source, position);
            CREATE INDEX IF NOT EXISTS entries_category ON entries(category, source, position);
            CREATE INDEX IF NOT EXISTS entries_table ON entries(table_name, source, position);
        ''')
        # Dates follow the host's local timezone, just like the source reader.
        zone = json.dumps([time.tzname, time.timezone, time.daylight, os.environ.get('TZ')])
        old = connection.execute("SELECT value FROM settings WHERE key='timezone'").fetchone()
        if old != (zone,):
            connection.execute('DELETE FROM sources')
            connection.execute("INSERT OR REPLACE INTO settings VALUES ('timezone',?)", (zone,))

    def _sync(self, connection, selected_date=None, dates_only=False):
        from .audit import _record_date
        paths = self.audit._log_paths()
        sources = {('0:' if p == self.audit.legacy_path else '1:') + p.name: p for p in paths}
        saved = dict((row[0], row[1:]) for row in connection.execute('SELECT * FROM sources'))
        for removed in saved.keys() - sources.keys():
            connection.execute('DELETE FROM sources WHERE source=?', (removed,))
        for source, path in sources.items():
            if path != self.audit.legacy_path:
                if dates_only or (selected_date and path.name != selected_date.isoformat() + '.jsonl'):
                    continue
            task_checkpoint(f'检查日志 {path.name}')
            try:
                with path.open('rb') as stream:
                    stat = os.fstat(stream.fileno())
                    identity = f'{stat.st_dev}:{stat.st_ino}'
                    old = saved.get(source)
                    if old and old[:3] == (identity, stat.st_size, stat.st_mtime_ns):
                        continue
                    offset = 0
                    if old and old[0] == identity and stat.st_size > old[1]:
                        # Verify the prior append boundary before trusting the old prefix.
                        stream.seek(max(0, old[3] - 256))
                        if stream.read(min(256, old[3])) == old[4]:
                            offset = old[3]
                    if not offset:
                        connection.execute('DELETE FROM sources WHERE source=?', (source,))
                        connection.execute('INSERT INTO sources(source) VALUES (?)', (source,))
                    else:
                        # A valid final JSON value without LF is provisional until appended.
                        connection.execute('DELETE FROM entries WHERE source=? AND position>=?', (source, offset))
                    stream.seek(offset)
                    checkpoint, pending = offset, []
                    while stream.tell() < stat.st_size:
                        position = stream.tell()
                        raw = stream.readline(stat.st_size - position)
                        if not raw:
                            raise OSError('日志在建立索引时被截断')
                        if raw.endswith(b'\n'): checkpoint = stream.tell()
                        record = self._parse(raw)
                        if record is None: continue
                        payload = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
                        pending.append((source, position, _record_date(record),
                            record.get('category') if isinstance(record.get('category'), str) else None,
                            record.get('status') if isinstance(record.get('status'), str) else None,
                            str(record.get('table') or ''), str(record.get('operation') or '').casefold(),
                            payload))
                        if len(pending) >= 500:
                            task_checkpoint(f'建立日志索引：{path.name}', stream.tell(), stat.st_size)
                            connection.executemany('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)', pending)
                            pending.clear()
                    if pending:
                        task_checkpoint(f'建立日志索引：{path.name}', stream.tell(), stat.st_size)
                        connection.executemany('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)', pending)
                    stream.seek(max(0, checkpoint - 256))
                    tail = stream.read(min(256, checkpoint))
                    connection.execute('UPDATE sources SET identity=?,size=?,mtime=?,offset=?,tail=? WHERE source=?',
                        (identity, stat.st_size, stat.st_mtime_ns, checkpoint, tail, source))
            except FileNotFoundError:
                connection.execute('DELETE FROM sources WHERE source=?', (source,))

    def _try_readonly(self, action, *, selected_date=None, dates_only=False):
        """源文件均未变化时走无写锁只读快路径，提升并发日志查询性能。"""
        if not self.path.is_file():
            return False, None
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2)) as connection:
                self._register_functions(connection)
                connection.execute('BEGIN')
                ver = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
                if ver != (self._SCHEMA_VERSION,):
                    return False, None
                zone = json.dumps([time.tzname, time.timezone, time.daylight, os.environ.get('TZ')])
                old_zone = connection.execute("SELECT value FROM settings WHERE key='timezone'").fetchone()
                if old_zone != (zone,):
                    return False, None
                paths = self.audit._log_paths()
                sources = {('0:' if p == self.audit.legacy_path else '1:') + p.name: p for p in paths}
                saved = {row[0]: row[1:] for row in connection.execute('SELECT source,identity,size,mtime FROM sources')}
                if saved.keys() - sources.keys():
                    return False, None
                for source, path in sources.items():
                    if path != self.audit.legacy_path:
                        if dates_only or (selected_date and path.name != selected_date.isoformat() + '.jsonl'):
                            continue
                    info = path.stat()
                    identity = f'{info.st_dev}:{info.st_ino}'
                    if saved.get(source) != (identity, info.st_size, info.st_mtime_ns):
                        return False, None
                return True, action(connection)
        except (OSError, sqlite3.Error):
            return False, None

    def _run(self, action, *, selected_date=None, dates_only=False):
        hit, cached = self._try_readonly(action, selected_date=selected_date, dates_only=dates_only)
        if hit:
            return cached
        # Index construction must not hold the JSONL append lock or delay audited writes.
        with _locked(self.lock_path, timeout=2):
            for attempt in range(2):
                try:
                    with closing(sqlite3.connect(self.path, timeout=2, isolation_level=None)) as connection:
                        self._register_functions(connection)
                        connection.execute('PRAGMA foreign_keys=ON')
                        connection.execute('PRAGMA journal_mode=WAL')
                        connection.execute('PRAGMA synchronous=NORMAL')
                        self._schema(connection)
                        connection.execute('BEGIN IMMEDIATE')
                        try:
                            self._sync(connection, selected_date, dates_only)
                            result = action(connection)
                            connection.commit()
                            return result
                        except BaseException:
                            connection.rollback()
                            raise
                except sqlite3.DatabaseError as error:
                    code = getattr(error, 'sqlite_errorcode', 0) & 255
                    if attempt or code not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                        raise
                    # Only this disposable index and its own journals are removed.
                    for path in (self.path, self.path.with_name(self.path.name + '-wal'),
                                 self.path.with_name(self.path.name + '-shm')):
                        path.unlink(missing_ok=True)

    def _dates(self, connection):
        from .audit import _DATE_FILE
        dates = set()
        for path in self.audit.log_dir.glob('*.jsonl'):
            match = _DATE_FILE.fullmatch(path.name)
            if match:
                try: dates.add(self.audit._validate_date(match[1]).isoformat())
                except ValueError: pass
        dates.update(row[0] for row in connection.execute(
            "SELECT DISTINCT record_date FROM entries WHERE source=? AND record_date<>''",
            ('0:' + self.audit.legacy_path.name,)))
        return sorted(dates, reverse=True)

    def available_dates(self):
        return self._run(self._dates, dates_only=True)

    def query(self, *, selected_date, page, page_size, category, status, keyword, table, operation):
        def select(connection):
            task_checkpoint('过滤和分页查询日志')
            conditions, args = [], []
            if selected_date:
                day = selected_date.isoformat()
                conditions.extend(['record_date=?', 'source IN (?,?)'])
                args.extend([day, '1:' + day + '.jsonl', '0:' + self.audit.legacy_path.name])
            for column, value in (('category', category), ('status', status), ('table_name', table)):
                if value:
                    conditions.append(column + '=?'); args.append(value)
            if keyword:
                conditions.append('instr(rtdb_casefold(payload),?)>0'); args.append(keyword.casefold())
            if operation:
                conditions.append('instr(operation,?)>0'); args.append(operation.casefold())
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
            total = connection.execute('SELECT COUNT(*) FROM entries' + where, args).fetchone()[0]
            records = [json.loads(row[0]) for row in connection.execute(
                'SELECT payload FROM entries' + where + ' ORDER BY source DESC,position DESC LIMIT ? OFFSET ?',
                [*args, page_size, (page - 1)*page_size])]
            return dict(logs=records, count=len(records), total=total, page=page, page_size=page_size,
                        pages=max(1, (total + page_size - 1)//page_size),
                        date=selected_date.isoformat() if selected_date else '', dates=self._dates(connection))
        return self._run(select, selected_date=selected_date)
