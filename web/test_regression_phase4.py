"""Phase 4 Web 服务层 P0/P1 回归测试。"""
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from web import database_snapshots, receipt_maintenance
from web.audit import OperationAuditLog
from web.errors import APIError
from web.management import SQLiteManagement
from web.sqlite_server import SQLiteRequestHandler
from web.sqlite_service import SQLiteManager
from web.tasks import TaskRegistry
from web.workspace import WorkspaceCatalog
from web.write_receipts import AuditDelivery


def _device_schema(name='devices'):
    return {
        'head': {'eng_name': name, 'chn_name': '设备表'},
        'columns': [
            {'eng_name': 'id', 'chn_name': '编号', 'data_type': 'int', 'primary_key': True, 'disp_type': '整数', 'disp_menu': ''},
            {'eng_name': 'name', 'chn_name': '名称', 'data_type': 'str', 'primary_key': False, 'disp_type': '文本', 'disp_menu': ''},
        ],
    }


class WebPhase4RegressionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='rtdb-p4-web-')
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / 'data'
        self.schema_dir = self.root / 'schemas'
        self.data_dir.mkdir()
        self.schema_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_origin_null_rejected_on_write_request(self):
        """校验 Origin: null（如 sandbox iframe / file://）写请求必须被 403 拦截。"""
        handler = object.__new__(SQLiteRequestHandler)
        handler.server = SimpleNamespace(server_address=('127.0.0.1', 8766))
        handler.path = '/api/manage/info'
        handler.headers = {'Origin': 'null', 'Host': '127.0.0.1:8766'}
        with self.assertRaises(APIError) as ctx:
            handler._route()
        self.assertEqual(ctx.exception.status, 403)

        # 合法同源放行
        handler.headers = {'Origin': 'http://127.0.0.1:8766', 'Host': '127.0.0.1:8766'}
        path, parts, _query = handler._route()
        self.assertEqual(path, '/api/manage/info')
        self.assertEqual(parts, ['api', 'manage', 'info'])

    def test_database_snapshots_scope_preserves_read_only_flag(self):
        """database_snapshots.scope 必须保留原 manager 的 read_only 状态，不可悄悄提权为可写。"""
        manager = SQLiteManager(self.data_dir, self.schema_dir)
        try:
            manager.create_database_file({'name': 'station', 'chn_name': '站点', 'tables': [_device_schema()]})
            ro_manager = copy.copy(manager)
            ro_manager.read_only = True
            scoped = database_snapshots.scope(ro_manager, 'station.db')
            self.assertTrue(scoped.read_only)
            self.assertEqual(scoped.database_file, 'station.db')
        finally:
            manager.close()

    def test_audit_index_readonly_fast_path_and_incremental_refresh(self):
        """审计日志文件未变更时应走无写锁只读快路径，新增记录后应自动增量同步。"""
        audit = OperationAuditLog(self.data_dir)
        audit.append(category='data', operation='init_op', status='success', http_status=200,
                     method='POST', path='/api/manage/test', table='devices', result={'count': 1})
        first = audit.query(keyword='init_op')
        self.assertEqual(first['total'], 1)

        # 未变更再次查询：断言不触发写锁重建 _sync
        with patch('web.audit_index.AuditIndex._sync', side_effect=AssertionError('未变更不应进入写锁重建路径')):
            cached = audit.query(keyword='init_op')
        self.assertEqual(cached['total'], 1)

        # 追加新日志后，只读快路径检测到文件变化并自动增量同步
        audit.append(category='data', operation='second_op', status='success', http_status=200,
                     method='POST', path='/api/manage/test2', table='devices', result={'count': 2})
        updated = audit.query(keyword='second_op')
        self.assertEqual(updated['total'], 1)

    def test_workspace_catalog_lru_evicts_idle_contexts_beyond_64(self):
        """WorkspaceCatalog 超过 64 个空闲子目录时自动按 LRU 淘汰最久未用目录而不报 503。"""
        default_dir = self.root / 'default'
        default_dir.mkdir()
        default_schema = default_dir / '.rtdb-schema'
        default_mgr = SQLiteManager(default_dir, default_schema)
        default_tasks = TaskRegistry(default_dir / '.web-tasks', default_mgr)
        default_delivery = default_mgr.audit_delivery = AuditDelivery(default_mgr)
        default_ctx = SimpleNamespace(
            sqlite_manager=default_mgr,
            management=SQLiteManagement(default_mgr),
            tasks=default_tasks,
            audit_delivery=default_delivery,
        )
        catalog = WorkspaceCatalog(self.root, default=default_ctx)
        try:
            for idx in range(66):
                sub = self.root / f'ws_{idx:03d}'
                sub.mkdir()
                ctx = catalog.get(f'ws_{idx:03d}')
                self.assertIsNotNone(ctx)
            self.assertEqual(len(catalog.contexts), 64)
            # 最早的 ws_000 与 ws_001 已被淘汰，最新的 ws_065 在缓存尾部
            self.assertNotIn((self.root / 'ws_000').resolve(), catalog.contexts)
            self.assertNotIn((self.root / 'ws_001').resolve(), catalog.contexts)
            self.assertIn((self.root / 'ws_065').resolve(), catalog.contexts)
        finally:
            catalog.close()
            default_tasks.close()
            default_delivery.close()
            default_mgr.close()

    def test_receipt_maintenance_none_result_and_non_overlapping_statistics(self):
        """验证正常返回 None 的回执不会误判为归档墓碑，且统计口径 pending/delivered/archived 互不重叠。"""
        manager = SQLiteManager(self.data_dir, self.schema_dir)
        try:
            manager.create_database_file({'name': 'rcpt', 'chn_name': '回执库', 'tables': [_device_schema()]})
            scoped = manager.for_database('rcpt.db')
            db_path = self.data_dir / 'rcpt.db'
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    f'CREATE TABLE {receipt_maintenance.TABLE} '
                    '(id TEXT PRIMARY KEY, result TEXT NOT NULL, event TEXT NOT NULL, delivered INTEGER NOT NULL)'
                )
                conn.execute(
                    f'CREATE TABLE {receipt_maintenance.ARCHIVE} '
                    '(id TEXT PRIMARY KEY, body BLOB NOT NULL, digest TEXT NOT NULL, original_bytes INTEGER NOT NULL)'
                )
                # 1 条待同步、1 条已同步且 result='null' 但非墓碑（正常返回 None）、1 条归档墓碑
                conn.execute(
                    f'INSERT INTO {receipt_maintenance.TABLE} VALUES (?,?,?,?)',
                    ('r_pending', json.dumps({'ok': True}), json.dumps({'timestamp': '2026-01-01T00:00:00Z'}), 0),
                )
                conn.execute(
                    f'INSERT INTO {receipt_maintenance.TABLE} VALUES (?,?,?,?)',
                    ('r_none', 'null', json.dumps({'timestamp': '2026-01-02T00:00:00Z', 'operation': 'noop'}), 1),
                )
                conn.execute(
                    f'INSERT INTO {receipt_maintenance.TABLE} VALUES (?,?,?,?)',
                    ('r_archived', 'null', '{}', 1),
                )
                conn.execute(
                    f'INSERT INTO {receipt_maintenance.ARCHIVE} VALUES (?,?,?,?)',
                    ('r_archived', b'x', '0' * 64, 10),
                )
                # 正常返回 None 的回执不应触发归档缺失 409，应原样返回 None
                self.assertIsNone(receipt_maintenance.decode_result(conn, 'r_none', 'null'))
                # 归档墓碑且压缩体损坏应抛出 409
                with self.assertRaises(APIError) as ctx:
                    receipt_maintenance.decode_result(conn, 'r_archived', 'null')
                self.assertEqual(ctx.exception.status, 409)

            stats = receipt_maintenance.statistics(scoped)
            self.assertEqual(stats['pending'], 1)
            self.assertEqual(stats['delivered'], 1)
            self.assertEqual(stats['archived'], 1)
        finally:
            manager.close()


if __name__ == '__main__':
    unittest.main()
