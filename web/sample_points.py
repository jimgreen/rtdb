"""独立侧库采样点定义与后台历史趋势采集服务。

参考 SCADA 采样点与曲线板（PointTrendHistory）设计：
- 不侵入业务 SQLite 库，在 <data_dir>/.rtdb-trends/<hash>.sqlite3 独立保存采样配置与样本；
- 支持任意 RTDB 标准表及原生 SQLite 表的数值 / 布尔字段按主键定位采集（每库最多 24 点）；
- 后台守护线程按固定节拍批量只读采样、自动按留存期裁剪并执行增量 vacuum；
- 提供增量游标查询（after_ms + 均匀降采样）与基于 revision / If-Match 的乐观并发更新。
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import copy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from core.sqlite_db import _BOOL, _FLOAT, _INT
from .errors import APIError
from .sqlite_service import browser_value

LOG = logging.getLogger(__name__)

MAX_POINTS = 24
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_RETENTION_SECONDS = 3600
MAX_INTERVAL_SECONDS = 60
MAX_RETENTION_SECONDS = 86400
MAX_RESULTS = 7200
_PRUNE_INTERVAL = 30.0
_VACUUM_PAGES = 16
_APPLICATION_ID = 0x52544442  # ASCII 'RTDB'
_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
_STEP_CHOICES = (0, 5, 15, 30, 60)

DEFAULT_PALETTE = (
    '#2563EB', '#0D9488', '#EA580C', '#7C3AED',
    '#DC2626', '#0284C7', '#16A34A', '#CA8A04',
    '#DB2777', '#4F46E5', '#0891B2', '#65A30D',
)


def _utc_ms() -> int:
    return int(time.time() * 1000)


def _int_value(value, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise APIError(400, f'{name} 必须为整数')
    if not minimum <= value <= maximum:
        raise APIError(400, f'{name} 必须在 {minimum} 到 {maximum} 之间')
    return value


def _float_or_none(value, name: str):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise APIError(400, f'{name} 必须为有效数值')
    result = float(value)
    if not math.isfinite(result):
        raise APIError(400, f'{name} 必须为有限数值')
    return result


def normalize_point(raw) -> dict:
    """规范化并校验单个采样点定位标识：{ table, key, field }。"""
    if not isinstance(raw, dict):
        raise APIError(400, '采样点定义必须是对象')
    table = raw.get('table')
    field = raw.get('field')
    key = raw.get('key')
    if not isinstance(table, str) or not table.strip() or len(table) > 128:
        raise APIError(400, '采样点表名无效')
    if not isinstance(field, str) or not field.strip() or len(field) > 128:
        raise APIError(400, '采样点字段名无效')
    if not isinstance(key, list) or not 1 <= len(key) <= 16:
        raise APIError(400, '采样点主键必须是包含 1–16 项的数组')
    clean_key = []
    for item in key:
        if isinstance(item, bool):
            clean_key.append(item)
        elif isinstance(item, int):
            clean_key.append(item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise APIError(400, '采样点主键值不能为非有限浮点')
            clean_key.append(item)
        elif isinstance(item, str):
            if len(item) > 512:
                raise APIError(400, '采样点主键字符串过长')
            clean_key.append(item)
        else:
            raise APIError(400, '采样点主键值类型不支持')
    return {'table': table.strip(), 'key': clean_key, 'field': field.strip()}


def point_key(point: dict) -> str:
    """返回采样点的稳定唯一键（用于去重与历史样本索引）。"""
    return json.dumps(
        [point['table'], point['key'], point['field']],
        ensure_ascii=False,
        separators=(',', ':'),
    )


def default_settings() -> dict:
    """默认采样配置与曲线视图设置。"""
    return {
        'interval_seconds': DEFAULT_INTERVAL_SECONDS,
        'retention_seconds': DEFAULT_RETENTION_SECONDS,
        'series': [],
        'view': {
            'open': True,
            'range_mode': 'rolling',
            'window_minutes': 15,
            'step_seconds': 0,
            'from': '',
            'until': '',
            'axes': {
                'left': {'auto': True, 'min': None, 'max': None, 'unit': ''},
                'right': {'auto': True, 'min': None, 'max': None, 'unit': ''},
            },
        },
    }


def _validate_axis(raw, current: dict, label: str) -> dict:
    if raw is None:
        return copy.deepcopy(current)
    if not isinstance(raw, dict):
        raise APIError(400, f'{label} 纵轴配置必须是对象')
    auto = bool(raw.get('auto', current.get('auto', True)))
    min_val = _float_or_none(raw.get('min', current.get('min')), f'{label}最小值')
    max_val = _float_or_none(raw.get('max', current.get('max')), f'{label}最大值')
    if not auto and min_val is not None and max_val is not None and min_val >= max_val:
        raise APIError(400, f'{label}最小值必须小于最大值')
    unit = raw.get('unit', current.get('unit', ''))
    if not isinstance(unit, str):
        raise APIError(400, f'{label}单位必须是字符串')
    return {
        'auto': auto,
        'min': min_val,
        'max': max_val,
        'unit': unit.strip()[:24],
    }


def _validate_view(raw, current: dict) -> dict:
    base = default_settings()['view']
    if isinstance(current, dict):
        base.update({k: copy.deepcopy(v) for k, v in current.items() if k in base})
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise APIError(400, 'view 视图配置必须是对象')
    range_mode = raw.get('range_mode', base['range_mode'])
    if range_mode not in ('rolling', 'fixed'):
        raise APIError(400, '时间区间模式仅支持 rolling 或 fixed')
    window_minutes = _int_value(raw.get('window_minutes', base['window_minutes']), '窗口时长（分钟）', 1, 1440)
    step_seconds = _int_value(raw.get('step_seconds', base['step_seconds']), '对齐步长（秒）', 0, 60)
    if step_seconds not in _STEP_CHOICES:
        raise APIError(400, '对齐步长仅支持 0/5/15/30/60 秒')
    from_text = raw.get('from', base['from'])
    until_text = raw.get('until', base['until'])
    if not isinstance(from_text, str) or len(from_text) > 40:
        raise APIError(400, '开始时间格式无效')
    if not isinstance(until_text, str) or len(until_text) > 40:
        raise APIError(400, '结束时间格式无效')
    axes_raw = raw.get('axes')
    if axes_raw is not None and not isinstance(axes_raw, dict):
        raise APIError(400, 'axes 纵轴配置必须是对象')
    axes_raw = axes_raw or {}
    return {
        'open': bool(raw.get('open', base['open'])),
        'range_mode': range_mode,
        'window_minutes': window_minutes,
        'step_seconds': step_seconds,
        'from': from_text.strip(),
        'until': until_text.strip(),
        'axes': {
            'left': _validate_axis(axes_raw.get('left'), base['axes']['left'], '左轴'),
            'right': _validate_axis(axes_raw.get('right'), base['axes']['right'], '右轴'),
        },
    }


def validate_settings(body, current: dict | None = None) -> dict:
    """校验并规范化完整采样点设置与视图参数。"""
    if not isinstance(body, dict):
        raise APIError(400, '采样配置必须是 JSON 对象')
    base = copy.deepcopy(current) if isinstance(current, dict) else default_settings()
    interval = _int_value(
        body.get('interval_seconds', base.get('interval_seconds', DEFAULT_INTERVAL_SECONDS)),
        '采样周期（秒）', 1, MAX_INTERVAL_SECONDS,
    )
    retention = _int_value(
        body.get('retention_seconds', base.get('retention_seconds', DEFAULT_RETENTION_SECONDS)),
        '后台留存时长（秒）', 1, MAX_RETENTION_SECONDS,
    )
    raw_series = body.get('series', base.get('series', []))
    if not isinstance(raw_series, list):
        raise APIError(400, 'series 采样点列表必须是数组')
    if len(raw_series) > MAX_POINTS:
        raise APIError(400, f'单个数据库最多定义 {MAX_POINTS} 个采样点')

    seen: set[str] = set()
    series: list[dict] = []
    for index, item in enumerate(raw_series):
        if not isinstance(item, dict):
            raise APIError(400, '采样点配置项必须是对象')
        point = normalize_point(item.get('point'))
        pkey = point_key(point)
        if pkey in seen:
            continue
        seen.add(pkey)
        name = item.get('name')
        if not isinstance(name, str) or not name.strip():
            key_text = ','.join(str(v) for v in point['key'])
            name = f"{point['table']}[{key_text}].{point['field']}"
        else:
            name = name.strip()[:120]
        unit = item.get('unit', '')
        if not isinstance(unit, str):
            unit = ''
        unit = unit.strip()[:24]
        kind = item.get('kind', 'analog')
        if kind not in ('analog', 'switch'):
            kind = 'analog'
        axis = item.get('axis', 'left')
        if axis not in ('left', 'right'):
            raise APIError(400, '纵轴仅支持 left 或 right')
        color = item.get('color') or DEFAULT_PALETTE[index % len(DEFAULT_PALETTE)]
        if not isinstance(color, str) or not _COLOR_RE.fullmatch(color.strip()):
            color = DEFAULT_PALETTE[index % len(DEFAULT_PALETTE)]
        else:
            color = color.strip()
        width = _int_value(item.get('width', 2), '曲线线宽', 1, 6)
        series.append({
            'key': pkey,
            'point': point,
            'name': name,
            'unit': unit,
            'kind': kind,
            'visible': bool(item.get('visible', True)),
            'axis': axis,
            'color': color,
            'width': width,
        })

    view = _validate_view(body.get('view'), base.get('view', {}))
    return {
        'interval_seconds': interval,
        'retention_seconds': retention,
        'series': series,
        'view': view,
    }


def _to_numeric(raw, is_bool: bool):
    """将数据库字段值转为可绘图的有限数值，无法转换时返回 (None, False)。"""
    if raw is None:
        return None, False
    if isinstance(raw, bool):
        return (1.0 if raw else 0.0), True
    if is_bool:
        if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
            return (1.0 if float(raw) != 0.0 else 0.0), True
        text = str(raw).strip().lower()
        if text in ('true', '1', 'yes', 'on'):
            return 1.0, True
        if text in ('false', '0', 'no', 'off'):
            return 0.0, True
        return None, False
    if isinstance(raw, (int, float)):
        val = float(raw)
        return (val, True) if math.isfinite(val) else (None, False)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, False
        try:
            val = float(text)
        except ValueError:
            return None, False
        return (val, True) if math.isfinite(val) else (None, False)
    return None, False


def read_sample_points(manager, points: list[dict]) -> dict[str, dict]:
    """按表分组在单次只读事务中批量读取采样点当前值。

    返回值按 point_key 索引：
    {
      "<point_key>": {
        "value": 220.5,
        "raw": 220.5,
        "valid": True,
        "found": True,
        "kind": "analog" | "switch",
        "quality": 0,   # 0 正常；1 无效/空值；2 缺失/不可读
        "time_ms": 1730000000000
      }
    }
    """
    if not points:
        return {}
    if len(points) > MAX_POINTS:
        raise APIError(400, f'单次最多读取 {MAX_POINTS} 个采样点')

    now_ms = _utc_ms()
    grouped: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for raw_point in points:
        pt = normalize_point(raw_point)
        grouped[pt['table']].append((point_key(pt), pt))

    results: dict[str, dict] = {}
    for table_name, items in grouped.items():
        try:
            with manager.session(table_name) as table, table._read_transaction() as connection:
                columns = {col.eng_name: col for col in table.definition.columns}
                lookup_map: dict[str, object] = {}
                key_errors: dict[str, str] = {}
                for pkey, pt in items:
                    try:
                        coerced = manager._key(table, pt['key'])
                        pk_tuple = tuple(coerced[k] for k in table.primary_keys)
                        lookup_map[pkey] = pk_tuple[0] if len(pk_tuple) == 1 else pk_tuple
                    except Exception as error:
                        key_errors[pkey] = str(error)

                lookup_keys = list(dict.fromkeys(lookup_map.values()))
                rows = table.kv._get_many(connection, lookup_keys) if lookup_keys else {}

                for pkey, pt in items:
                    if pkey in key_errors:
                        results[pkey] = {
                            'value': None, 'raw': None, 'valid': False, 'found': False,
                            'kind': 'analog', 'quality': 2, 'time_ms': now_ms,
                            'error': key_errors[pkey],
                        }
                        continue
                    col = columns.get(pt['field'])
                    if col is None:
                        results[pkey] = {
                            'value': None, 'raw': None, 'valid': False, 'found': False,
                            'kind': 'analog', 'quality': 2, 'time_ms': now_ms,
                            'error': f'字段不存在：{pt["field"]}',
                        }
                        continue
                    dtype = col.data_type.strip().lower()
                    is_bool = dtype in _BOOL
                    kind = 'switch' if is_bool else 'analog'
                    row = rows.get(lookup_map[pkey])
                    if row is None:
                        results[pkey] = {
                            'value': None, 'raw': None, 'valid': False, 'found': False,
                            'kind': kind, 'quality': 2, 'time_ms': now_ms,
                        }
                        continue
                    raw_val = row.get(pt['field'])
                    numeric, valid = _to_numeric(raw_val, is_bool)
                    results[pkey] = {
                        'value': numeric,
                        'raw': browser_value(raw_val),
                        'valid': valid,
                        'found': True,
                        'kind': kind,
                        'quality': 0 if valid else 1,
                        'time_ms': now_ms,
                    }
        except Exception as error:
            for pkey, _ in items:
                results[pkey] = {
                    'value': None, 'raw': None, 'valid': False, 'found': False,
                    'kind': 'analog', 'quality': 2, 'time_ms': now_ms,
                    'error': str(error),
                }
    return results


def _downsample(samples: list[dict], limit: int = MAX_RESULTS) -> list[dict]:
    """当样本数超出上限时均匀降采样，同时保证保留首尾节点。"""
    total = len(samples)
    if total <= limit:
        return samples
    if limit <= 2:
        return [samples[0], samples[-1]]
    step = (total - 1) / (limit - 1)
    indices = [round(i * step) for i in range(limit)]
    indices[-1] = total - 1
    deduped = list(dict.fromkeys(indices))
    return [samples[i] for i in deduped]


class _FileLock:
    """轻量跨平台侧库进程排他锁，防止多进程同时写同一采样侧库。"""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                if os.path.getsize(self.path) == 0:
                    handle.write(b'\x00')
                    handle.flush()
                    handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise APIError(409, '该数据库的采样历史侧库正被另一进程占用') from error
        self._handle = handle

    def release(self):
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass


class SampleTrendHistory:
    """单个数据库的采样点定义持久化与后台定时采集器。"""

    def __init__(self, manager, db_key: str, storage_path: Path):
        self._manager = manager
        self.db_key = db_key
        self.storage_path = storage_path
        self._lock = threading.RLock()
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None
        self._file_lock = _FileLock(storage_path.with_suffix('.sqlite3.lock'))
        self._conn: sqlite3.Connection | None = None
        self._settings = default_settings()
        self._revision = self._compute_revision(self._settings)
        self._last_prune = 0.0
        self._sample_immediately = False

        self._open_storage()
        if self._settings['series']:
            self._ensure_thread_locked()

    @staticmethod
    def _compute_revision(settings: dict, nonce: int = 0) -> str:
        payload = json.dumps({'settings': settings, 'nonce': nonce}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]

    def _open_storage(self):
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_lock.acquire()
        try:
            conn = sqlite3.connect(str(self.storage_path), check_same_thread=False, timeout=5.0)
            try:
                conn.execute('PRAGMA busy_timeout=5000')
                app_id = conn.execute('PRAGMA application_id').fetchone()[0]
                page_count = conn.execute('PRAGMA page_count').fetchone()[0]
                if page_count == 0:
                    conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
                    conn.execute(f'PRAGMA application_id={_APPLICATION_ID}')
                elif app_id not in (0, _APPLICATION_ID):
                    raise APIError(409, '采样历史侧库文件签名不匹配，已拒绝打开')
                elif app_id == 0:
                    conn.execute(f'PRAGMA application_id={_APPLICATION_ID}')
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA synchronous=NORMAL')
                conn.execute(
                    'CREATE TABLE IF NOT EXISTS trend_settings ('
                    'id INTEGER PRIMARY KEY CHECK (id = 1), '
                    'settings TEXT NOT NULL, '
                    'revision TEXT NOT NULL, '
                    'updated_ms INTEGER NOT NULL)'
                )
                conn.execute(
                    'CREATE TABLE IF NOT EXISTS trend_sample ('
                    'time_ms INTEGER PRIMARY KEY, '
                    'samples TEXT NOT NULL)'
                )
                row = conn.execute('SELECT settings, revision FROM trend_settings WHERE id = 1').fetchone()
                if row is not None:
                    try:
                        loaded = json.loads(row[0])
                        self._settings = validate_settings(loaded, default_settings())
                        self._revision = str(row[1]) if row[1] else self._compute_revision(self._settings)
                    except Exception:
                        LOG.warning('采样配置损坏，已回退至默认配置：%s', self.storage_path)
                        self._settings = default_settings()
                        self._revision = self._compute_revision(self._settings)
                else:
                    self._settings = default_settings()
                    self._revision = self._compute_revision(self._settings)
                    conn.execute(
                        'INSERT INTO trend_settings (id, settings, revision, updated_ms) VALUES (1, ?, ?, ?)',
                        (json.dumps(self._settings, ensure_ascii=False), self._revision, _utc_ms()),
                    )
                    conn.commit()
                self._conn = conn
            except BaseException:
                conn.close()
                raise
        except BaseException:
            self._file_lock.release()
            raise

    def _ensure_thread_locked(self):
        if self._closed or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f'rtdb-sample-trend-{self.db_key[:24]}',
            daemon=True,
        )
        self._thread.start()

    @property
    def is_sampling(self) -> bool:
        with self._lock:
            return (not self._closed) and bool(self._settings['series'])

    def _sample_once(self) -> None:
        with self._lock:
            if self._closed or self._conn is None:
                return
            series = copy.deepcopy(self._settings['series'])
            retention_ms = self._settings['retention_seconds'] * 1000
        if not series:
            self._maybe_prune(retention_ms, force=False)
            return

        points = [item['point'] for item in series]
        readings = read_sample_points(self._manager, points)
        sample_ms = _utc_ms()
        compact_samples = {}
        for item in series:
            pkey = item['key']
            reading = readings.get(pkey)
            if reading is None:
                continue
            compact_samples[pkey] = {
                'v': reading['value'],
                'q': reading['quality'],
                'ok': bool(reading['valid']),
                'f': bool(reading['found']),
            }

        with self._lock:
            if self._closed or self._conn is None:
                return
            try:
                self._conn.execute(
                    'INSERT OR REPLACE INTO trend_sample (time_ms, samples) VALUES (?, ?)',
                    (sample_ms, json.dumps(compact_samples, ensure_ascii=False, separators=(',', ':'))),
                )
                self._conn.commit()
            except sqlite3.Error as error:
                LOG.warning('写入采样样本失败 (%s): %s', self.db_key, error)
                return
        self._maybe_prune(retention_ms, force=False)

    def _maybe_prune(self, retention_ms: int, *, force: bool = False) -> None:
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_prune) < _PRUNE_INTERVAL:
            return
        cutoff = _utc_ms() - retention_ms
        with self._lock:
            if self._closed or self._conn is None:
                return
            self._last_prune = now_mono
            try:
                cursor = self._conn.execute('DELETE FROM trend_sample WHERE time_ms < ?', (cutoff,))
                deleted = cursor.rowcount
                if deleted and deleted > 0:
                    self._conn.commit()
                    self._conn.execute(f'PRAGMA incremental_vacuum({_VACUUM_PAGES})')
            except sqlite3.Error as error:
                LOG.warning('裁剪过期采样样本失败 (%s): %s', self.db_key, error)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                interval = self._settings['interval_seconds']
                has_series = bool(self._settings['series'])
                immediate = self._sample_immediately
                self._sample_immediately = False

            if not has_series:
                # 无采样点时不空转，等待新配置唤醒或关闭信号。
                self._wakeup.wait(timeout=30.0)
                self._wakeup.clear()
                continue

            if immediate:
                try:
                    self._sample_once()
                except Exception as error:
                    LOG.warning('后台即时采样异常 (%s): %s', self.db_key, error)

            now = time.time()
            next_tick = (math.floor(now / interval) + 1) * interval
            wait_seconds = max(0.05, next_tick - now)
            woken = self._wakeup.wait(timeout=wait_seconds)
            self._wakeup.clear()
            if self._stop.is_set():
                break
            if woken:
                continue
            try:
                self._sample_once()
            except Exception as error:
                LOG.warning('后台定时采样异常 (%s): %s', self.db_key, error)

    def get(self, after_ms: int = 0) -> dict:
        """读取当前采样配置与时间大于 after_ms 的历史采样点数据。"""
        if after_ms < 0:
            after_ms = 0
        with self._lock:
            if self._closed or self._conn is None:
                raise APIError(503, '采样服务已关闭')
            settings = copy.deepcopy(self._settings)
            revision = self._revision
            retention_ms = settings['retention_seconds'] * 1000
            cutoff = max(after_ms, _utc_ms() - retention_ms)
            rows = self._conn.execute(
                'SELECT time_ms, samples FROM trend_sample WHERE time_ms > ? ORDER BY time_ms ASC',
                (cutoff,),
            ).fetchall()

        samples = []
        for ts, raw_json in rows:
            try:
                parsed = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
            samples.append({'time_ms': ts, 'samples': parsed})
        samples = _downsample(samples, MAX_RESULTS)
        return {
            'database': self.db_key if self.db_key != '__default__' else None,
            'settings': settings,
            'samples': samples,
            'revision': revision,
            'server_time_ms': _utc_ms(),
            'max_points': MAX_POINTS,
        }

    def put(self, body: dict, *, expected_revision: str | None = None) -> dict:
        """乐观并发更新采样配置、周期与视图参数，并立即触发一次采集。"""
        with self._lock:
            if self._closed or self._conn is None:
                raise APIError(503, '采样服务已关闭')
            if expected_revision is not None:
                cleaned = expected_revision.strip().strip('"')
                if cleaned and cleaned != '*' and cleaned != self._revision:
                    raise APIError(
                        409,
                        '采样点配置已被其他会话修改，已刷新最新配置，请核对后重试',
                        details=self.get(0),
                    )
            validated = validate_settings(body, self._settings)
            new_revision = self._compute_revision(validated, _utc_ms())
            self._conn.execute(
                'INSERT OR REPLACE INTO trend_settings (id, settings, revision, updated_ms) VALUES (1, ?, ?, ?)',
                (json.dumps(validated, ensure_ascii=False), new_revision, _utc_ms()),
            )
            cutoff = _utc_ms() - validated['retention_seconds'] * 1000
            self._conn.execute('DELETE FROM trend_sample WHERE time_ms < ?', (cutoff,))
            self._conn.commit()
            self._settings = validated
            self._revision = new_revision
            has_series = bool(validated['series'])
            if has_series:
                self._ensure_thread_locked()
            self._wakeup.set()

        # 若新增或保留了采样点，同步快速采集首帧，让前端立即可见当前值。
        if has_series:
            try:
                self._sample_once()
            except Exception as error:
                LOG.warning('保存采样配置后即时采集失败 (%s): %s', self.db_key, error)
        return self.get(0)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            self._wakeup.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        with self._lock:
            conn = self._conn
            self._conn = None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._file_lock.release()


class SampleTrendRegistry:
    """按工作区目录管理每个数据库对应的独立 SampleTrendHistory 实例。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.trends_dir = self.data_dir / '.rtdb-trends'
        self._lock = threading.RLock()
        self._histories: dict[str, SampleTrendHistory] = {}
        self._closed = False

    def _storage_path(self, db_key: str) -> Path:
        digest = hashlib.sha256(db_key.encode('utf-8')).hexdigest()[:32]
        return self.trends_dir / f'{digest}.sqlite3'

    def for_manager(self, manager) -> SampleTrendHistory:
        db_key = manager.database_file or '__default__'
        with self._lock:
            if self._closed:
                raise APIError(503, '采样注册表已关闭')
            existing = self._histories.get(db_key)
            if existing is not None:
                return existing
            # 保存独立的 manager 浅拷贝用于后台定时采集，不受请求级状态影响。
            scoped_manager = copy.copy(manager)
            history = SampleTrendHistory(scoped_manager, db_key, self._storage_path(db_key))
            self._histories[db_key] = history
            return history

    def has_active_samplers(self) -> bool:
        with self._lock:
            return any(history.is_sampling for history in self._histories.values())

    def close(self) -> None:
        with self._lock:
            self._closed = True
            histories = list(self._histories.values())
            self._histories.clear()
        for history in histories:
            try:
                history.close()
            except Exception:
                pass
