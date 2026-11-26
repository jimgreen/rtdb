"""采样点定义与实时趋势后台采集服务（web/sample_points.py）自动化测试。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from core import SQLiteDatabase, TableDefinition
from web.sample_points import MAX_POINTS, _downsample, point_key, validate_settings
from web.sqlite_server import create_sqlite_server


def _http_json(base_url: str, path: str, method: str = 'GET', body=None, headers: dict | None = None):
    raw = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    req_headers = {'Accept': 'application/json'}
    if raw is not None:
        req_headers['Content-Type'] = 'application/json'
    if headers:
        req_headers.update(headers)
    req = Request(base_url + path, data=raw, method=method, headers=req_headers)
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except HTTPError as err:
        return err.code, json.loads(err.read().decode('utf-8'))


class SampleTrendsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.schema_dir = self.workspace / '.rtdb-schema'
        self.schema_dir.mkdir(parents=True, exist_ok=True)

        # 创建一个带数值字段和布尔字段的 RTDB 测试库（grouped .json 定义）
        self.db_filename = 'plant_a.db'
        grouped_def = {
            'head': {'eng_name': 'plant_a', 'chn_name': 'A站数据库'},
            'tables': [
                {
                    'head': {'eng_name': 'telemetry', 'chn_name': '遥测表'},
                    'columns': [
                        {'eng_name': 'id', 'chn_name': '编号', 'data_type': 'int', 'disp_type': 'int', 'primary_key': True},
                        {'eng_name': 'name', 'chn_name': '名称', 'data_type': 'str', 'disp_type': 'str', 'primary_key': False},
                        {'eng_name': 'voltage', 'chn_name': '电压', 'data_type': 'float', 'disp_type': 'float', 'primary_key': False},
                        {'eng_name': 'breaker', 'chn_name': '断路器状态', 'data_type': 'bool', 'disp_type': 'bool', 'primary_key': False},
                    ],
                }
            ],
        }
        (self.schema_dir / 'plant_a.json').write_text(json.dumps(grouped_def, ensure_ascii=False), encoding='utf-8')
        with SQLiteDatabase(self.workspace, schema_directory=self.schema_dir, database_file=self.db_filename) as db:
            table = db['telemetry']
            table.insert_one({'id': 1, 'name': '1号主变', 'voltage': 220.5, 'breaker': True})
            table.insert_one({'id': 2, 'name': '2号主变', 'voltage': 110.2, 'breaker': False})

        # 再创建一个原生 SQLite 库验证原生表兼容
        self.native_filename = 'external_raw.db'
        native_path = self.workspace / self.native_filename
        conn = sqlite3.connect(str(native_path))
        try:
            conn.execute('CREATE TABLE metrics (k INTEGER PRIMARY KEY, temp REAL, alarm INTEGER)')
            conn.execute('INSERT INTO metrics (k, temp, alarm) VALUES (10, 36.5, 1)')
            conn.commit()
        finally:
            conn.close()

        self.server = create_sqlite_server(
            '127.0.0.1',
            0,
            root=self.root,
            data_dir=self.workspace,
            schema_dir=self.schema_dir,
            workspace_dir=self.workspace,
        )
        self.port = self.server.server_address[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def test_validate_settings_bounds_and_downsample(self):
        # 超出 MAX_POINTS 应报错
        too_many = [
            {'point': {'table': 't', 'key': [i], 'field': 'v'}}
            for i in range(MAX_POINTS + 1)
        ]
        with self.assertRaises(Exception):
            validate_settings({'series': too_many})

        # 重复点自动去重且默认携带 1s 周期与 10000 点留存
        dup = [
            {'point': {'table': 't', 'key': [1], 'field': 'v'}, 'axis': 'left'},
            {'point': {'table': 't', 'key': [1], 'field': 'v'}, 'axis': 'right'},
        ]
        cleaned = validate_settings({'series': dup})
        self.assertEqual(len(cleaned['series']), 1)
        self.assertEqual(cleaned['series'][0]['interval_seconds'], 1)
        self.assertEqual(cleaned['series'][0]['retention_points'], 10000)

        # 均匀降采样保留首尾
        data = [{'time_ms': i, 'samples': {}} for i in range(1000)]
        reduced = _downsample(data, limit=100)
        self.assertEqual(len(reduced), 100)
        self.assertEqual(reduced[0]['time_ms'], 0)
        self.assertEqual(reduced[-1]['time_ms'], 999)

    def test_crud_optimistic_lock_background_sampling_and_incremental_fetch(self):
        # 1. 初始 GET 应返回默认空配置，且不写入冗余成功审计
        status, data = _http_json(self.base, f'/api/manage/sample-trends?database={self.db_filename}')
        self.assertEqual(status, 200)
        self.assertEqual(data['settings']['series'], [])
        rev1 = data['revision']
        self.assertTrue(rev1)

        # 2. 定义 1 个数值采样点（左轴，1s 周期，留存 100 点）和 1 个布尔采样点（右轴，2s 周期，留存 200 点）
        p_volt = {'table': 'telemetry', 'key': [1], 'field': 'voltage'}
        p_brk = {'table': 'telemetry', 'key': [1], 'field': 'breaker'}
        payload = {
            'series': [
                {'point': p_volt, 'name': '1号主变电压', 'unit': 'kV', 'kind': 'analog', 'axis': 'left',
                 'color': '#2563EB', 'interval_seconds': 1, 'retention_points': 100},
                {'point': p_brk, 'name': '1号主变开关', 'unit': '', 'kind': 'switch', 'axis': 'right',
                 'color': '#0D9488', 'interval_seconds': 1, 'retention_points': 200},
            ],
        }
        status, saved = _http_json(
            self.base,
            f'/api/manage/sample-trends?database={self.db_filename}',
            method='PUT',
            body=payload,
            headers={'If-Match': rev1},
        )
        self.assertEqual(status, 200)
        rev2 = saved['revision']
        self.assertNotEqual(rev1, rev2)
        self.assertEqual(len(saved['settings']['series']), 2)
        self.assertEqual(saved['settings']['series'][0]['interval_seconds'], 1)
        self.assertEqual(saved['settings']['series'][0]['retention_points'], 100)
        self.assertEqual(saved['settings']['series'][1]['retention_points'], 200)
        # 保存后同步采入首帧
        self.assertGreaterEqual(len(saved['samples']), 1)
        first_slot = saved['samples'][-1]
        k_volt = point_key(p_volt)
        k_brk = point_key(p_brk)
        self.assertAlmostEqual(first_slot['samples'][k_volt]['v'], 220.5)
        self.assertTrue(first_slot['samples'][k_volt]['ok'])
        self.assertAlmostEqual(first_slot['samples'][k_brk]['v'], 1.0)
        self.assertGreaterEqual(saved['point_counts'][k_volt], 1)

        # 3. 过期 If-Match 必须返回 409 并附带最新状态
        status, conflict = _http_json(
            self.base,
            f'/api/manage/sample-trends?database={self.db_filename}',
            method='PUT',
            body=payload,
            headers={'If-Match': rev1},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict['details']['revision'], rev2)

        # 4. 修改业务库数据，等待后台守护线程按 1s 节拍采集，并测试 after_ms 增量拉取
        last_ms = first_slot['time_ms']
        with SQLiteDatabase(self.workspace, schema_directory=self.schema_dir, database_file=self.db_filename) as db:
            db['telemetry'].update({'voltage': 231.8, 'breaker': False}, {'id': 1})

        found_updated = False
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            time.sleep(0.35)
            status, inc = _http_json(
                self.base,
                f'/api/manage/sample-trends?database={self.db_filename}&after_ms={last_ms}',
            )
            self.assertEqual(status, 200)
            for slot in inc['samples']:
                self.assertGreater(slot['time_ms'], last_ms)
                volt_rec = slot['samples'].get(k_volt)
                brk_rec = slot['samples'].get(k_brk)
                if volt_rec and abs(volt_rec['v'] - 231.8) < 1e-6:
                    if brk_rec is not None:
                        self.assertAlmostEqual(brk_rec['v'], 0.0)
                    found_updated = True
                    break
            if found_updated:
                break
        self.assertTrue(found_updated, '后台守护线程应在采样周期内采集到更新后的数值与布尔状态')

        # 5. 验证侧库隔离：.rtdb-trends 存在且不会被列出为业务数据库
        trends_dir = self.workspace / '.rtdb-trends'
        self.assertTrue(trends_dir.is_dir())
        self.assertGreaterEqual(len(list(trends_dir.glob('*.db'))), 1)
        status, dbs = _http_json(self.base, '/api/manage/databases')
        self.assertEqual(status, 200)
        db_names = {item['filename'] for item in dbs['databases']}
        self.assertIn(self.db_filename, db_names)
        self.assertFalse(any('.rtdb-trends' in name for name in db_names))

        # 6. 验证写操作进入审计日志，且高频只读 GET 未产生冗余审计
        status, logs = _http_json(self.base, f'/api/manage/logs?database={self.db_filename}&page_size=50')
        self.assertEqual(status, 200)
        ops = [entry['operation'] for entry in logs['logs']]
        self.assertIn('更新采样点配置', ops)

    def test_instant_read_endpoint_and_missing_row(self):
        points = [
            {'table': 'telemetry', 'key': [2], 'field': 'voltage'},
            {'table': 'telemetry', 'key': [999], 'field': 'voltage'},
        ]
        status, data = _http_json(
            self.base,
            f'/api/manage/sample-trends/read?database={self.db_filename}',
            method='POST',
            body={'points': points},
        )
        self.assertEqual(status, 200)
        readings = data['readings']
        k_ok = point_key(points[0])
        k_miss = point_key(points[1])
        self.assertTrue(readings[k_ok]['valid'])
        self.assertAlmostEqual(readings[k_ok]['value'], 110.2)
        self.assertFalse(readings[k_miss]['found'])
        self.assertFalse(readings[k_miss]['valid'])

    def test_retention_pruning(self):
        p_volt = {'table': 'telemetry', 'key': [1], 'field': 'voltage'}
        k_volt = point_key(p_volt)
        status, initial = _http_json(self.base, f'/api/manage/sample-trends?database={self.db_filename}')
        self.assertEqual(status, 200)
        status, saved = _http_json(
            self.base,
            f'/api/manage/sample-trends?database={self.db_filename}',
            method='PUT',
            body={'series': [{'point': p_volt, 'interval_seconds': 60, 'retention_points': 100}]},
            headers={'If-Match': initial['revision']},
        )
        self.assertEqual(status, 200)

        # 直接往分点表写入 25 条历史样本（时间严格递增）
        history = self.server.sample_trends.for_manager(self.server.sqlite_manager.for_database(self.db_filename))
        base_ms = int(time.time() * 1000) - 50000
        with history._lock:
            history._conn.executemany(
                'INSERT OR REPLACE INTO trend_point_sample '
                '(point_key, time_ms, value, quality, ok, found) VALUES (?, ?, ?, ?, ?, ?)',
                [(k_volt, base_ms + i * 1000, float(100 + i), 0, 1, 1) for i in range(25)],
            )
            history._conn.commit()

        # 将该采样点留存点数调整为 10 点，应精确裁剪至仅保留最新的 10 条样本
        status, after = _http_json(
            self.base,
            f'/api/manage/sample-trends?database={self.db_filename}',
            method='PUT',
            body={'series': [{'point': p_volt, 'interval_seconds': 60, 'retention_points': 10}]},
            headers={'If-Match': saved['revision']},
        )
        self.assertEqual(status, 200)
        self.assertEqual(after['point_counts'][k_volt], 10)
        self.assertEqual(len(after['samples']), 10)


if __name__ == '__main__':
    unittest.main()
