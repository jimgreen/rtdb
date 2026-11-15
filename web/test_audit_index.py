"""Indexed browsing must preserve the JSONL reader's exact observable results."""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from web.audit import OperationAuditLog, _record_date


def concurrent_log_work(root, writing, ready, output):
    try:
        audit = OperationAuditLog(root)
        ready.wait(10)
        for _ in range(20):
            if writing:
                audit.append(category='data', operation='并发写入', status='success', http_status=200,
                             method='POST', path='/test')
            else:
                audit.query(page_size=3)
        output.put(None)
    except BaseException as error:
        output.put(repr(error))


class AuditIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audit = OperationAuditLog(self.root)
        self.day = '2026-11-03'
        self.path = self.audit.log_dir / (self.day + '.jsonl')
        self.rows = [dict(id=str(i), timestamp=self.day + 'T12:00:00+13:00',
                         category='schema' if i % 3 else 'data', status='failure' if i % 2 else 'success',
                         table='节点', operation='修改 Straße', detail='断线 %_') for i in range(17)]
        self.write(self.path, self.rows)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, rows):
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')

    def oracle(self, **kwargs):
        records = []
        selected = self.audit._validate_date(kwargs.get('date_value'))
        filters = {k: kwargs.get(k) for k in ('category', 'status', 'table', 'operation', 'keyword')}
        for path in self.audit._log_paths(selected):
            for line in reversed(path.read_text(encoding='utf-8', errors='replace').splitlines()):
                try: record = json.loads(line)
                except json.JSONDecodeError: continue
                if not isinstance(record, dict): continue
                if selected and _record_date(record) != selected.isoformat(): continue
                if self.audit._matches(record, **filters): records.append(record)
        page, size = kwargs.get('page', 1), kwargs.get('page_size', 50)
        return records[(page - 1)*size:page*size], len(records)

    def assert_query(self, **kwargs):
        expected, total = self.oracle(**kwargs)
        actual = self.audit.query(**kwargs)
        self.assertEqual(actual['logs'], expected)
        self.assertEqual(actual['total'], total)
        return actual

    def test_filters_pages_unicode_and_legacy_order_match_source(self):
        self.write(self.audit.legacy_path, [*self.rows[:3], {'id': 'old', 'timestamp': '2001-02-03T12:00:00+13:00'}])
        with self.path.open('a', encoding='utf-8') as stream: stream.write('bad json\n[]\nnull\n')
        for args in ({}, {'page': 3, 'page_size': 4}, {'page': 99}, {'date_value': self.day},
                     {'category': 'data', 'status': 'failure'}, {'table': '节点'},
                     {'keyword': 'STRASSE'}, {'keyword': '%_'}, {'operation': 'STRASSE'},
                     {'date_value': '2001-02-03'}, {'date_value': '2000-01-01'}):
            with self.subTest(args=args): self.assert_query(**args)
        self.assertIn('2001-02-03', self.audit.query()['dates'])

    def test_warm_query_and_new_owner_do_not_reparse_history(self):
        self.assert_query()
        from web.audit_index import AuditIndex
        with patch.object(AuditIndex, '_parse', side_effect=AssertionError('history reparsed')):
            self.audit = OperationAuditLog(self.root)
            self.assert_query(page_size=3)

    def test_incremental_append_only_parses_new_lines(self):
        self.audit.query()
        from web.audit_index import AuditIndex
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({**self.rows[0], 'id': 'new'}) + '\n')
        with patch.object(AuditIndex, '_parse', wraps=AuditIndex._parse) as parse:
            self.assert_query()
        self.assertEqual(parse.call_count, 1)

    def test_partial_last_line_and_missing_newline(self):
        raw = json.dumps(self.rows[0], ensure_ascii=False).encode('utf-8')
        self.path.write_bytes(raw[:20]); self.assert_query()
        with self.path.open('ab') as stream: stream.write(raw[20:])
        self.assert_query()
        with self.path.open('ab') as stream: stream.write(b'\n' + raw + b'\n')
        self.assert_query()

    def test_rewrite_truncate_replace_and_delete(self):
        self.audit.query()
        self.write(self.path, [{**r, 'id': 'x' + r['id']} for r in self.rows]); self.assert_query()
        self.write(self.path, self.rows[:2]); self.assert_query()
        other = self.path.with_suffix('.replacement')
        self.write(other, self.rows[3:6]); other.replace(self.path); self.assert_query()
        self.path.unlink(); self.assert_query()

    def test_corrupt_or_unwritable_index_keeps_logs_readable(self):
        self.audit.query()
        from web.audit_index import AuditIndex
        index = AuditIndex(self.audit)
        index.path.write_bytes(b'not a sqlite database')
        self.assert_query()
        with patch.object(AuditIndex, 'query', side_effect=OSError('index unavailable')):
            self.assert_query(status='failure')
        from core.file_mapped_db import StorageError
        with patch('web.audit_index._locked', side_effect=StorageError('lock unavailable')):
            self.assert_query(status='failure')

    def test_retention_removes_cached_history(self):
        self.audit.query()
        self.audit.set_retention(1)
        self.assertFalse(self.path.exists())
        self.assert_query()

    def test_file_date_and_record_date_both_filter(self):
        misplaced = {**self.rows[0], 'timestamp': '2026-11-04T12:00:00+13:00'}
        self.write(self.path, [misplaced])
        self.assert_query(date_value='2026-11-04')
        self.assert_query(date_value=self.day)

    def test_available_dates_uses_cached_legacy_records(self):
        self.write(self.audit.legacy_path, self.rows)
        self.audit.query()
        from web.audit_index import AuditIndex
        with patch.object(AuditIndex, '_parse', side_effect=AssertionError('legacy reparsed')):
            self.assertIn(self.day, self.audit.available_dates())

    def test_external_owner_append_is_visible(self):
        self.audit.query()
        other = OperationAuditLog(self.root)
        result = other.append(category='data', operation='新增', status='success', http_status=200,
                              method='POST', path='/test')
        self.assertIn(result, self.audit.query()['logs'])

    def test_date_list_does_not_parse_dated_files(self):
        from web.audit_index import AuditIndex
        with patch.object(AuditIndex, '_parse', side_effect=AssertionError('unnecessary scan')):
            self.assertEqual(self.audit.available_dates(), [self.day])

    def test_selected_date_only_indexes_relevant_files(self):
        self.write(self.audit.log_dir / '2026-11-02.jsonl', self.rows)
        from web.audit_index import AuditIndex
        with patch.object(AuditIndex, '_parse', wraps=AuditIndex._parse) as parse:
            self.assert_query(date_value=self.day)
        self.assertEqual(parse.call_count, len(self.rows))

    def test_concurrent_process_queries_and_appends(self):
        context = multiprocessing.get_context('spawn')
        ready, output = context.Event(), context.Queue()
        processes = [context.Process(target=concurrent_log_work, args=(self.root, writing, ready, output))
                     for writing in (True, False, False)]
        try:
            for process in processes: process.start()
            ready.set()
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual([output.get(timeout=2) for _ in processes], [None]*3)
            result = self.assert_query()
            self.assertEqual(result['total'], len(self.rows) + 20)
        finally:
            for process in processes:
                if process.is_alive(): process.terminate(); process.join()
            output.close()

    def test_index_construction_does_not_block_log_append(self):
        from web.audit_index import AuditIndex
        entered, release = threading.Event(), threading.Event()
        original = AuditIndex._parse
        def paused(raw):
            entered.set(); release.wait(5)
            return original(raw)
        with ThreadPoolExecutor(2) as pool, patch.object(AuditIndex, '_parse', side_effect=paused):
            reading = pool.submit(self.audit.query)
            try:
                self.assertTrue(entered.wait(2))
                writing = pool.submit(self.audit.append, category='data', operation='写入', status='success',
                                      http_status=200, method='POST', path='/test')
                self.assertIsNotNone(writing.result(timeout=2))
            finally:
                release.set()
            reading.result(timeout=5)
        self.assertEqual(self.assert_query()['total'], len(self.rows) + 1)


if __name__ == '__main__': unittest.main()
