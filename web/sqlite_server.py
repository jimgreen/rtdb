"""SQLite-specific management WEB service.

This endpoint is intentionally separate from the fixed-mmap service: it
exposes SQLite table names, primary-key row editing, schema
version checks, safe read-only SQL, controlled writes, import/export and
integrity checks through one browser-oriented API.
"""
from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .errors import APIError
from .audit import status_for_exception
from .sqlite_service import SQLiteManager, browser_value
from .management import SQLiteManagement
from .sample_points import SampleTrendRegistry, read_sample_points
from .tasks import TaskRegistry
from core import SchemaError
from core.sqlite_db import SQLiteBusyError

STATIC_ROOT = Path(__file__).resolve().parent / 'sqlite_static'
MAX_BODY = 32 * 1024 * 1024

_FILE_PATH_FIELDS = {
    'workspace_root', 'default_data_dir', 'data_dir', 'schema_dir',
    'database_path', 'schema_path', 'audit_log', 'audit_log_legacy',
    'audit_log_config', 'legacy_path', 'json_path', 'python_path',
    'path', 'archive', 'backup',
}
_METADATA_CONTAINERS = {
    'file', 'files', 'databases', 'tables', 'summary', 'task', 'tasks', 'result',
    'results', 'logs', 'details', 'delivery', 'failures', 'protected',
}
_DIAGNOSTIC_FIELDS = {'error', 'reason', 'export_warning'}


def _relative_file_path(value, root):
    path = Path(value)
    if not path.is_absolute():
        windows_path = PureWindowsPath(value)
        if windows_path.anchor or '..' in windows_path.parts:
            return None
        return value
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return relative.as_posix() if '..' not in relative.parts else None


def _relative_metadata(payload, root):
    """Format server metadata without visiting database rows or definitions."""
    root_forms = (str(root), root.as_posix(), root.as_uri())
    prefixes = {form.rstrip('/\\') for form in root_forms} - {''}
    # 同时脱敏带斜杠的子路径和无尾斜杠的根目录本身，防止路径泄露。
    root_prefix = re.compile(
        r'(?:' + '|'.join(re.escape(prefix) for prefix in sorted(prefixes, key=len, reverse=True)) + r')(?:[/\\]|(?=$|[\s\'",;:)\]}]))',
        re.IGNORECASE if os.name == 'nt' else 0,
    )

    def format_item(value, *, diagnostic=False):
        if isinstance(value, str):
            return root_prefix.sub('', value) if diagnostic else value
        if isinstance(value, list):
            return [format_item(item, diagnostic=diagnostic) for item in value]
        if not isinstance(value, dict):
            return value
        formatted = {}
        for key, item in value.items():
            if key in _FILE_PATH_FIELDS and isinstance(item, str):
                # Task and audit "path" values identify HTTP routes.
                if key != 'path' or 'method' not in value:
                    item = _relative_file_path(item, root)
            elif key in _DIAGNOSTIC_FIELDS and isinstance(item, str):
                item = root_prefix.sub('', item)
            elif key in _METADATA_CONTAINERS:
                item = format_item(item, diagnostic=diagnostic or key == 'details')
            elif diagnostic:
                item = format_item(item, diagnostic=True)
            formatted[key] = item
        return formatted

    return format_item(payload)


class SQLiteRequestHandler(BaseHTTPRequestHandler):
    server_version = 'RTDBSQLiteWeb/1.0'
    protocol_version = 'HTTP/1.1'

    def _release_request_context(self):
        ctx = getattr(self, '_request_context', None)
        if ctx is not None:
            self._request_context = None
            self._request_manager = None
            workspaces = getattr(self.server, 'workspaces', None)
            if workspaces is not None and hasattr(workspaces, 'release'):
                workspaces.release(ctx)

    def handle_one_request(self):
        self._request_context = None
        self._request_manager = None
        try:
            super().handle_one_request()
        finally:
            self._release_request_context()

    @property
    def context(self):
        if self._request_context is None:
            relative = parse_qs(urlsplit(self.path).query).get('workspace', [''])[0]
            workspaces = self.server.workspaces
            self._request_context = workspaces.acquire(relative) if hasattr(workspaces, 'acquire') else workspaces.get(relative)
        return self._request_context

    @property
    def manager(self):
        cached = getattr(self, '_request_manager', None)
        if cached is not None:
            return cached
        manager = self.context.sqlite_manager
        filename = parse_qs(urlsplit(self.path).query).get('database', [None])[0]
        resolved = manager.for_database(filename) if filename else manager
        self._request_manager = resolved
        return resolved

    def _should_log_request(self, format, args):
        if os.environ.get('RTDB_ACCESS_LOG') == '1':
            return True
        if len(args) >= 2:
            req_line = str(args[0])
            status = str(args[1])
            if status.startswith(('2', '3')):
                parts = req_line.split(' ')
                if len(parts) >= 2:
                    method, raw_path = parts[0], parts[1]
                    path_only = urlsplit(raw_path).path
                    if method == 'GET' and (
                        path_only.startswith('/static/')
                        or path_only in ('/', '/index.html', '/favicon.ico', '/api/health')
                        or path_only in ('/api/manage/audit-delivery', '/api/manage/receipt-maintenance', '/api/manage/logs', '/api/manage/sample-trends')
                        or '/tasks/' in path_only
                    ):
                        return False
        return True

    def log_message(self, format, *args):
        if not self._should_log_request(format, args):
            return
        sys.stderr.write('%s - - [%s] %s\n' % (self.address_string(), self.log_date_time_string(), format % args))

    def _maybe_gzip(self, body):
        if len(body) >= 1024 and 'gzip' in self.headers.get('Accept-Encoding', '').lower():
            return gzip.compress(body, compresslevel=5), True
        return body, False

    def _send_json(self, status, payload):
        encoding_started=time.perf_counter()
        workspace = getattr(self.server, 'workspaces', None)
        if getattr(workspace, 'strict_root', False):
            payload = _relative_metadata(payload, workspace.root)
        if workspace is not None and isinstance(payload, dict) and (
            'workspace_root' in payload or payload.get('engine') == 'sqlite'
        ):
            payload.update(strict_root=workspace.strict_root, root_id=workspace.root_id)
        raw_body = json.dumps(browser_value(payload), ensure_ascii=False, allow_nan=False).encode('utf-8')
        body, compressed = self._maybe_gzip(raw_body)
        encoding_ms=(time.perf_counter()-encoding_started)*1000
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        if compressed:
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if hasattr(self, '_server_ms'):
            extra=''.join(f', {key[:-3]};dur={value:.3f}' for key,value in getattr(self,'_task_timings',{}).items())
            self.send_header('Server-Timing', f'app;dur={self._server_ms:.2f}, encode;dur={encoding_ms:.3f}'+extra)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc):
        if isinstance(exc, APIError):
            self._send_json(exc.status, {'error': exc.message, 'details': exc.details})
        elif isinstance(exc, SQLiteBusyError):
            self._send_json(503, {'error': '数据库繁忙，请稍后重试'})
        elif isinstance(exc, SchemaError):
            self._send_json(422, {'error': str(exc)})
        elif isinstance(exc, (ValueError, TypeError, OverflowError, sqlite3.Error)):
            self._send_json(400, {'error': str(exc)})
        else:
            if getattr(self.server.workspaces, 'strict_root', False):
                self.log_error('Unhandled RTDB error: %s', exc)
                self._send_json(500, {'error': '服务器处理失败，请查看服务日志'})
            else:
                self._send_json(500, {'error': 'internal server error', 'details': str(exc)})

    def _route(self):
        self._release_request_context()
        if hasattr(self, '_server_ms'):
            delattr(self, '_server_ms')
        self._task_timings = {}
        # Keep the default local administration service off unrelated origins.
        host = self.headers.get('Host', '')
        if self.server.server_address[0] in ('127.0.0.1', '::1'):
            if urlsplit('http://' + host).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise APIError(403, '不允许的 Host')
        origin = self.headers.get('Origin')
        # 同时拒绝 Origin: null 或空 Host 绕过。
        if origin and (not host or urlsplit(origin).netloc != host):
            raise APIError(403, '不允许跨站访问管理接口')
        parsed = urlsplit(self.path)
        return parsed.path, [unquote(part) for part in parsed.path.split('/') if part], parse_qs(parsed.query, keep_blank_values=True)

    def _body(self, *, optional=False):
        raw_length = self.headers.get('Content-Length')
        if raw_length is None:
            if optional:
                return {}
            length = 0
        else:
            try:
                length = int(raw_length)
            except (ValueError, TypeError) as exc:
                raise APIError(400, 'Content-Length 必须为非负整数') from exc
        if length < 0:
            raise APIError(400, 'Content-Length 必须为非负整数')
        if optional and length == 0:
            return {}
        if self.headers.get_content_type() != 'application/json':
            raise APIError(415, '请使用 application/json')
        if length > MAX_BODY:
            raise APIError(413, '请求体超过 32 MiB')
        old_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(30)
            raw = self.rfile.read(length) if length else b'{}'
        finally:
            self.connection.settimeout(old_timeout)
        if length and len(raw) != length:
            raise APIError(400, '请求体传输中断，未完整接收')
        try:
            payload = json.loads(raw.decode('utf-8'))
            if not isinstance(payload, dict):
                raise APIError(400, '请求体必须是 JSON 对象')
            return payload
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(400, '请求体必须是 UTF-8 JSON') from exc

    def _direct_descriptor(self, method, parts, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        if parts == ['api', 'health']:
            return 'system', '检查服务健康状态', None
        if parts == ['api', 'sqlite', 'info']:
            return 'system', '加载服务信息', None
        if parts == ['api', 'sqlite', 'tables']:
            return 'structure', '加载表结构列表', None
        if parts == ['api', 'sqlite', 'sql']:
            return 'data', '执行 SQL', payload.get('table')
        if parts == ['api', 'sqlite', 'schemas']:
            definition = payload.get('definition', payload)
            name = definition.get('head', {}).get('eng_name') if isinstance(definition, dict) else None
            return 'structure', '添加表结构', name
        if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables']:
            table, action = parts[3], parts[4]
            if method == 'PUT' and action == 'row':
                return 'data', '修改单条数据', table
            if method == 'DELETE' and action == 'table':
                return 'structure', '删除表结构', table
            category, operation, _ = self.context.management.operation_descriptor(
                method, ['tables', table, action], payload)
            return category, operation, table
        return 'system', f'{method} 操作', None

    def _recorded(self, method, parts, payload, callback, *, status_code=200, skip_success_audit=False):
        category, operation, table = self._direct_descriptor(method, parts, payload)
        started = time.perf_counter()
        recorder = None
        try:
            recorder = self.manager
            result = callback()
        except Exception as error:
            target = recorder or (self._request_context.sqlite_manager if self._request_context is not None else None)
            if target is not None:
                target.record_operation(category=category, operation=operation, status='failure',
                                        http_status=status_for_exception(error), method=method,
                                        path=urlsplit(self.path).path, table=table,
                                        duration_ms=(time.perf_counter() - started) * 1000,
                                        error=error)
            raise
        if not skip_success_audit:
            recorder.record_operation(category=category, operation=operation, status='success',
                                      http_status=status_code, method=method,
                                      path=urlsplit(self.path).path, table=table,
                                      duration_ms=(time.perf_counter() - started) * 1000,
                                      result=result)
        return result

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, If-Match')
        self.end_headers()

    def do_GET(self):
        try:
            path, parts, query = self._route()
            if self._manage('GET', parts, query=query):
                return
            if path == '/favicon.ico':
                self.send_response(204); self.end_headers(); return
            if path in ('/', '/index.html'):
                self._static('index.html'); return
            if path.startswith('/static/'):
                self._static(unquote(path.removeprefix('/static/'))); return
            if parts == ['api', 'health']:
                self._send_json(200, self._recorded('GET', parts, None,
                    lambda: {'ok': True, 'service': 'rtdb-sqlite', **self.manager.info()}, skip_success_audit=True)); return
            if parts == ['api', 'sqlite', 'info']:
                self._send_json(200, self._recorded('GET', parts, None, self.manager.info, skip_success_audit=True)); return
            if parts == ['api', 'sqlite', 'tables']:
                self._send_json(200, self._recorded('GET', parts, None, self.manager.tables, skip_success_audit=True)); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'schema':
                def read_schema():
                    with self.manager.session(parts[3]) as table:
                        from .sqlite_service import fingerprint
                        definition = table.definition.to_mapping()
                    return {'name': parts[3], 'definition': definition, 'schema_version': fingerprint(definition)}
                self._send_json(200, self._recorded('GET', parts, None, read_schema, skip_success_audit=True)); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'rows':
                def read_rows():
                    limit = int(query.get('limit', ['100'])[0] or 100)
                    offset = int(query.get('offset', ['0'])[0] or 0)
                    return self.manager.rows(parts[3], where=query.get('where', [''])[0], order=query.get('order', [''])[0], limit=limit, offset=offset)
                result = self._recorded('GET', parts, None, read_rows, skip_success_audit=True)
                self._send_json(200, result); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'export':
                content, media, filename = self._recorded('GET', parts, None,
                    lambda: self.manager.export(parts[3], {k: v[0] for k, v in query.items()}))
                disposition = (f'attachment; filename="{filename}"' if filename.isascii()
                               else "attachment; filename*=UTF-8''" + quote(filename))
                self.send_response(200)
                self.send_header('Content-Type', media)
                self.send_header('Content-Disposition', disposition)
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(content)
                return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'check':
                self._send_json(200, self._recorded('GET', parts, None, lambda: self.manager.check(parts[3]))); return
            raise APIError(404, '路由不存在')
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        try:
            _, parts, query = self._route(); payload = self._body()
            if self._manage('POST', parts, query=query, payload=payload):
                return
            if parts == ['api', 'sqlite', 'sql']:
                self._send_json(200, self._recorded('POST', parts, payload, lambda: self.manager.sql(payload.get('table'), payload))); return
            if parts == ['api', 'sqlite', 'schemas']:
                definition = payload.get('definition', payload)
                name = definition.get('head', {}).get('eng_name') if isinstance(definition, dict) else None
                self._send_json(201, self._recorded('POST', parts, payload,
                    lambda: self.manager.save_schema(name, {'definition': definition}, creating=True), status_code=201)); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'rows':
                self._send_json(201, self._recorded('POST', parts, payload,
                    lambda: self.manager.insert(parts[3], payload), status_code=201)); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'clear':
                self._send_json(200, self._recorded('POST', parts, payload,
                    lambda: self.manager.clear(parts[3], payload))); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'import':
                self._send_json(201, self._recorded('POST', parts, payload,
                    lambda: self.manager.import_data(parts[3], payload), status_code=201)); return
            raise APIError(404, '路由不存在')
        except Exception as exc:
            self._error(exc)

    def do_PUT(self):
        try:
            _, parts, query = self._route()
            if len(parts)==5 and parts[:3]==['api','manage','database-uploads'] and parts[4]=='chunk':
                self._upload_chunk(parts[3],query)
                return
            payload = self._body()
            if self._manage('PUT', parts, query=query, payload=payload):
                return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'schema':
                self._send_json(200, self._recorded('PUT', parts, payload,
                    lambda: self.manager.save_schema(parts[3], payload, creating=False))); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'row':
                self._send_json(200, self._recorded('PUT', parts, payload,
                    lambda: self.manager.edit(parts[3], payload))); return
            raise APIError(404, '路由不存在')
        except Exception as exc:
            self._error(exc)

    def do_DELETE(self):
        try:
            _, parts, query = self._route(); payload = self._body(optional=True)
            if self._manage('DELETE', parts, query=query, payload=payload):
                return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'rows':
                self._send_json(200, self._recorded('DELETE', parts, payload,
                    lambda: self.manager.delete_rows(parts[3], payload))); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'table':
                self._send_json(200, self._recorded('DELETE', parts, payload,
                    lambda: self.manager.drop(parts[3], payload))); return
            raise APIError(404, '路由不存在')
        except Exception as exc:
            self._error(exc)

    def _manage(self, method, parts, *, query=None, payload=None):
        if parts[:2] != ['api', 'manage']:
            return False
        if parts == ['api', 'manage', 'workspaces'] and method == 'GET':
            self._send_json(200, self.server.workspaces.listing())
            return True
        if parts == ['api', 'manage', 'workspaces', 'directories'] and method == 'POST':
            started = time.perf_counter()
            default_manager = self.server.workspaces.default.sqlite_manager
            try:
                default_manager.require_writable()
                result = self.server.workspaces.create_directory(payload.get('parent'), payload.get('name'))
            except Exception as error:
                default_manager.record_operation(
                    category='system', operation='新建工作目录', status='failure',
                    http_status=status_for_exception(error), method='POST',
                    path='/api/manage/workspaces/directories',
                    duration_ms=(time.perf_counter() - started) * 1000, error=error,
                )
                raise
            default_manager.record_operation(
                category='system', operation='新建工作目录', status='success',
                http_status=201, method='POST',
                path='/api/manage/workspaces/directories',
                duration_ms=(time.perf_counter() - started) * 1000, result=result,
            )
            self._send_json(201, result)
            return True
        if parts == ['api', 'manage', 'sample-trends']:
            started = time.perf_counter()
            manager = self.manager
            history = self.context.sample_trends.for_manager(manager)
            if method == 'GET':
                raw_after = (query or {}).get('after_ms', ['0'])[0]
                try:
                    after_ms = int(raw_after or 0)
                except (TypeError, ValueError) as error:
                    raise APIError(400, 'after_ms 必须为整数') from error
                result = history.get(after_ms)
                self._server_ms = (time.perf_counter() - started) * 1000
                self._send_json(200, result)
                return True
            if method == 'PUT':
                expected_revision = self.headers.get('If-Match')
                try:
                    result = history.put(payload, expected_revision=expected_revision)
                except Exception as error:
                    manager.record_operation(
                        category='system', operation='更新采样点配置', status='failure',
                        http_status=status_for_exception(error), method='PUT',
                        path='/api/manage/sample-trends',
                        duration_ms=(time.perf_counter() - started) * 1000, error=error,
                    )
                    raise
                manager.record_operation(
                    category='system', operation='更新采样点配置', status='success',
                    http_status=200, method='PUT',
                    path='/api/manage/sample-trends',
                    duration_ms=(time.perf_counter() - started) * 1000,
                    result={'series_count': len(result.get('settings', {}).get('series', [])),
                            'interval_seconds': result.get('settings', {}).get('interval_seconds'),
                            'retention_seconds': result.get('settings', {}).get('retention_seconds')},
                )
                self._server_ms = (time.perf_counter() - started) * 1000
                self._send_json(200, result)
                return True
            raise APIError(405, '采样点接口仅支持 GET / PUT')
        if parts == ['api', 'manage', 'sample-trends', 'read'] and method == 'POST':
            started = time.perf_counter()
            points = payload.get('points') if isinstance(payload, dict) else None
            if not isinstance(points, list):
                raise APIError(400, 'points 必须是采样点数组')
            readings = read_sample_points(self.manager, points)
            self._server_ms = (time.perf_counter() - started) * 1000
            self._send_json(200, {'readings': readings})
            return True
        if len(parts)==3 and parts[2]=='tasks' and method=='GET':
            self._send_json(200,{'tasks':self.context.tasks.recent()})
            return True
        if len(parts) in (4, 5) and parts[2] == 'tasks':
            filename = (query or {}).get('database', [None])[0]
            if method == 'GET' and len(parts) == 4:
                if 'wait' in (query or {}):
                    try:
                        timeout=float(query['wait'][0]);revision=int(query.get('revision',['0'])[0])
                    except (ValueError,TypeError):raise APIError(400,'无效的任务等待参数')
                    result=self.context.tasks.wait_change(parts[3],filename,revision,timeout)
                else:result = self.context.tasks.get(parts[3], filename)
            elif method == 'POST' and len(parts) == 5 and parts[4] == 'cancel':
                result = self.context.tasks.cancel(parts[3], filename)
            else:
                raise APIError(400, '无效的后台任务操作')
            self._send_json(200, result)
            return True
        if len(parts) >= 3 and parts[2] == 'database-snapshots':
            from .database_snapshots import scope
            manager = scope(self.context.sqlite_manager, (query or {}).get('database', [None])[0])
        else:
            manager = self.manager
        if method == 'GET' and len(parts) == 5 and parts[2] == 'database-snapshots' and parts[4] == 'download':
            from .storage_maintenance import fence
            with fence(manager):
                self._download_snapshot(manager, parts[3])
            return True
        management = SQLiteManagement(manager) if manager is not self.context.sqlite_manager else self.context.management
        if (query or {}).get('background') == ['1']:
            started=time.perf_counter()
            parameters = {key: values[0] for key, values in (query or {}).items() if key not in ('background','request_id','workspace')}
            result = self.context.tasks.submit(management, method, parts[2:], parameters, payload,
                request_id=(query or {}).get('request_id',[None])[0])
            result = self.context.tasks.wait_initial(result['id'], result['database'])
            self._server_ms=(time.perf_counter()-started)*1000
            self._task_timings=result.get('timings',{})
            if result.get('result_expired'):
                self._send_json(202,{'task':result})
                return True
            if result['status'] == 'done':
                self._send_json(200, result['result'])
                return True
            if result['status'] in ('failed', 'cancelled','interrupted'):
                raise APIError(result['error_status'], result['error'], details=result.get('details'))
            self._send_json(202, {'task': result})
            return True
        started = time.perf_counter()
        result = management.dispatch(method, parts[2:],
            {key: values[0] for key, values in (query or {}).items()}, payload)
        self._server_ms = (time.perf_counter() - started) * 1000
        self._send_json(200, result)
        return True

    def _upload_chunk(self, ident, query):
        from .database_uploads import CHUNK, put_chunk
        manager=self.context.sqlite_manager;started=time.perf_counter()
        try:
            if self.headers.get_content_type()!='application/octet-stream':raise APIError(415,'分块请使用 application/octet-stream')
            if 'Content-Length' not in self.headers:raise APIError(411,'分块需要 Content-Length')
            try:length=int(self.headers['Content-Length']);offset=int(query.get('offset',[''])[0])
            except (ValueError,TypeError):raise APIError(400,'分块长度或位置无效')
            if not 0<length<=CHUNK:raise APIError(413,'单个上传分块不能超过1 MiB')
            old_timeout=self.connection.gettimeout()
            try:
                self.connection.settimeout(30);body=self.rfile.read(length)
            finally:self.connection.settimeout(old_timeout)
            if len(body)!=length:raise APIError(400,'分块传输中断，未确认接收')
            result=put_chunk(manager,ident,offset,body,self.headers.get('X-Chunk-SHA256'))
            self._send_json(200,result)
        except Exception as error:
            manager.record_operation(category='structure',operation='上传分块失败',status='failure',http_status=status_for_exception(error),
                method='PUT',path=urlsplit(self.path).path,duration_ms=(time.perf_counter()-started)*1000,error=error)
            raise

    def _download_snapshot(self, manager, ident):
        from .database_snapshots import resolve, byte_range, CHUNK
        started = time.perf_counter(); sent = 0; headers_sent = False
        try:
            path, item = resolve(manager, ident)
            etag = '"' + item['sha256'] + '"'
            requested = self.headers.get('Range')
            if self.headers.get('If-Range', etag) != etag: requested = None
            try: start, end, partial = byte_range(requested, item['bytes'])
            except APIError as error:
                self.send_response(416); self.send_header('Content-Range', f'bytes */{item["bytes"]}')
                self.send_header('Content-Length', '0'); self.end_headers(); headers_sent = True
                raise error
            with path.open('rb') as stream:
                stream.seek(start)
                filename = item['filename']
                disposition = (f'attachment; filename="{filename}"' if filename.isascii()
                               else "attachment; filename*=UTF-8''" + quote(filename))
                self.send_response(206 if partial else 200)
                self.send_header('Content-Type', 'application/vnd.sqlite3')
                self.send_header('Content-Disposition', disposition)
                self.send_header('Content-Length', str(end - start + 1))
                self.send_header('Accept-Ranges', 'bytes'); self.send_header('ETag', etag)
                self.send_header('Cache-Control', 'no-store'); self.send_header('X-Content-Type-Options', 'nosniff')
                if partial: self.send_header('Content-Range', f'bytes {start}-{end}/{item["bytes"]}')
                self.end_headers(); headers_sent = True
                while sent < end - start + 1:
                    block = stream.read(min(CHUNK, end - start + 1 - sent))
                    if not block: raise OSError('快照文件读取提前结束')
                    self.wfile.write(block); sent += len(block)
            manager.record_operation(category='structure', operation='下载整库快照', status='success',
                http_status=206 if partial else 200, method='GET', path=urlsplit(self.path).path,
                duration_ms=(time.perf_counter()-started)*1000, result={'snapshot_id':ident,'bytes':sent,'partial':partial})
        except Exception as error:
            manager.record_operation(category='structure', operation='下载整库快照', status='failure',
                http_status=status_for_exception(error), method='GET', path=urlsplit(self.path).path,
                duration_ms=(time.perf_counter()-started)*1000, error=error)
            if not headers_sent: raise
            self.close_connection = True

    def _static(self, name):
        target = (STATIC_ROOT / name).resolve()
        if STATIC_ROOT.resolve() not in target.parents or not target.is_file():
            raise APIError(404, '文件不存在')
        stat_info = target.stat()
        etag = f'"{stat_info.st_mtime_ns:x}-{stat_info.st_size:x}"'
        if_none_match = self.headers.get('If-None-Match', '')
        if etag in [part.strip() for part in if_none_match.split(',')]:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            return
        media = {
            '.html': 'text/html; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
            '.svg': 'image/svg+xml; charset=utf-8',
        }.get(target.suffix.lower()) or (mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        raw_body = target.read_bytes()
        body, compressed = self._maybe_gzip(raw_body)
        self.send_response(200)
        self.send_header('Content-Type', media)
        if compressed:
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('ETag', etag)
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)


class SQLiteHTTPServer(ThreadingHTTPServer):
    # A reload can request many scripts while upload connections are closing.
    # Keep that brief burst queued instead of using socketserver's backlog of 5.
    request_queue_size = 64
    daemon_threads = True

    def server_close(self):
        super().server_close()
        if hasattr(self, 'sample_trends'):
            self.sample_trends.close()
        if hasattr(self, 'workspaces'):
            self.workspaces.close()
        if hasattr(self, 'tasks'):
            self.tasks.close()
        if hasattr(self, 'audit_delivery'):
            self.audit_delivery.close()
        if hasattr(self, 'sqlite_manager'):
            self.sqlite_manager.close()


def create_sqlite_server(host='127.0.0.1', port=8878, *, root=None, data_dir=None, schema_dir=None, workspace_dir=None):
    from .workspace import WorkspaceCatalog

    base = Path(root).resolve() if root else Path(__file__).resolve().parents[1]
    workspace_path = Path(workspace_dir) if workspace_dir is not None else Path('../workspace')
    catalog = WorkspaceCatalog(base / workspace_path, strict_root=workspace_dir is not None)
    if catalog.strict_root:
        data_dir, schema_dir = catalog.storage_paths(data_dir, schema_dir)
    manager = SQLiteManager(data_dir or base / 'sqlite_dat', schema_dir or base / 'def' / 'table',
                            strict_paths=catalog.strict_root)
    try:
        server = SQLiteHTTPServer((host, port), SQLiteRequestHandler)
    except BaseException:
        manager.close()
        raise
    server.sqlite_manager = manager  # type: ignore[attr-defined]
    server.management = SQLiteManagement(manager)
    server.workspaces = catalog
    catalog.default = server
    try:
        server.tasks = TaskRegistry(manager.data_dir/'.web-tasks', manager)
        from .write_receipts import AuditDelivery
        server.audit_delivery = manager.audit_delivery = AuditDelivery(manager)
        server.sample_trends = SampleTrendRegistry(manager.data_dir)
    except BaseException:
        server.server_close()
        raise
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='RTDB SQLite WEB management service')
    parser.add_argument('--host', default='127.0.0.1'); parser.add_argument('--port', type=int, default=8878)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1]); parser.add_argument('--data-dir', type=Path); parser.add_argument('--schema-dir', type=Path)
    parser.add_argument('--workspace-dir', type=Path, default=Path('../workspace'),
                        help='Web file root (relative to --root); data and schema directories must stay inside it')
    args = parser.parse_args(argv)
    server = create_sqlite_server(args.host, args.port, root=args.root, data_dir=args.data_dir, schema_dir=args.schema_dir, workspace_dir=args.workspace_dir)
    print(f'RTDB SQLite web service listening on http://{args.host}:{args.port}')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
