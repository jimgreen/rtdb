"""SQLite-specific management WEB service.

This endpoint is intentionally separate from the fixed-mmap service: it
exposes SQLite table names, primary-key row editing, schema
version checks, safe read-only SQL, controlled writes, import/export and
integrity checks through one browser-oriented API.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import APIError
from .audit import status_for_exception
from .sqlite_service import SQLiteManager, browser_value
from .management import SQLiteManagement
from core import SchemaError
from core.sqlite_db import SQLiteBusyError

STATIC_ROOT = Path(__file__).resolve().parent / 'sqlite_static'
MAX_BODY = 32 * 1024 * 1024


class SQLiteRequestHandler(BaseHTTPRequestHandler):
    server_version = 'RTDBSQLiteWeb/1.0'

    @property
    def manager(self):
        return self.server.sqlite_manager  # type: ignore[attr-defined]

    def log_message(self, format, *args):
        sys.stderr.write('%s - - [%s] %s\n' % (self.address_string(), self.log_date_time_string(), format % args))

    def _send_json(self, status, payload):
        body = json.dumps(browser_value(payload), ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
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
            self._send_json(500, {'error': 'internal server error', 'details': str(exc)})

    def _route(self):
        # Keep the default local administration service off unrelated origins.
        host = self.headers.get('Host', '')
        if self.server.server_address[0] in ('127.0.0.1', '::1'):
            if urlsplit('http://' + host).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise APIError(403, '不允许的 Host')
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != host:
            raise APIError(403, '不允许跨站访问管理接口')
        parsed = urlsplit(self.path)
        return parsed.path, [unquote(part) for part in parsed.path.split('/') if part], parse_qs(parsed.query, keep_blank_values=True)

    def _body(self):
        if self.headers.get_content_type() != 'application/json':
            raise APIError(415, '请使用 application/json')
        length = int(self.headers.get('Content-Length', '0'))
        if length < 0:
            raise APIError(400, 'Content-Length 必须为非负整数')
        if length > MAX_BODY:
            raise APIError(413, '请求体超过 32 MiB')
        try:
            payload = json.loads((self.rfile.read(length) if length else b'{}').decode('utf-8'))
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
            category, operation, _ = self.server.management.operation_descriptor(
                method, ['tables', table, action], payload)
            return category, operation, table
        return 'system', f'{method} 操作', None

    def _recorded(self, method, parts, payload, callback, *, status_code=200):
        category, operation, table = self._direct_descriptor(method, parts, payload)
        started = time.perf_counter()
        try:
            result = callback()
        except Exception as error:
            self.manager.record_operation(category=category, operation=operation, status='failure',
                                          http_status=status_for_exception(error), method=method,
                                          path=urlsplit(self.path).path, table=table,
                                          duration_ms=(time.perf_counter() - started) * 1000,
                                          error=error)
            raise
        self.manager.record_operation(category=category, operation=operation, status='success',
                                      http_status=status_code, method=method,
                                      path=urlsplit(self.path).path, table=table,
                                      duration_ms=(time.perf_counter() - started) * 1000,
                                      result=result)
        return result

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
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
                self._static(path.removeprefix('/static/')); return
            if parts == ['api', 'health']:
                self._send_json(200, self._recorded('GET', parts, None,
                    lambda: {'ok': True, 'service': 'rtdb-sqlite', **self.manager.info()})); return
            if parts == ['api', 'sqlite', 'info']:
                self._send_json(200, self._recorded('GET', parts, None, self.manager.info)); return
            if parts == ['api', 'sqlite', 'tables']:
                self._send_json(200, self._recorded('GET', parts, None, self.manager.tables)); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'schema':
                def read_schema():
                    with self.manager.session(parts[3]) as table:
                        from .sqlite_service import fingerprint
                        definition = table.definition.to_mapping()
                    return {'name': parts[3], 'definition': definition, 'schema_version': fingerprint(definition)}
                self._send_json(200, self._recorded('GET', parts, None, read_schema)); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'rows':
                def read_rows():
                    limit = int(query.get('limit', ['100'])[0] or 100)
                    offset = int(query.get('offset', ['0'])[0] or 0)
                    return self.manager.rows(parts[3], where=query.get('where', [''])[0], order=query.get('order', [''])[0], limit=limit, offset=offset)
                result = self._recorded('GET', parts, None, read_rows)
                self._send_json(200, result); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'export':
                content, media, filename = self._recorded('GET', parts, None,
                    lambda: self.manager.export(parts[3], {k: v[0] for k, v in query.items()}))
                self.send_response(200); self.send_header('Content-Type', media); self.send_header('Content-Disposition', f'attachment; filename="{filename}"'); self.send_header('Content-Length', str(len(content))); self.end_headers(); self.wfile.write(content); return
            if len(parts) == 5 and parts[:3] == ['api', 'sqlite', 'tables'] and parts[4] == 'check':
                self._send_json(200, self._recorded('GET', parts, None, lambda: self.manager.check(parts[3]))); return
            raise APIError(404, '路由不存在')
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        try:
            _, parts, _ = self._route(); payload = self._body()
            if self._manage('POST', parts, payload=payload):
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
            _, parts, _ = self._route(); payload = self._body()
            if self._manage('PUT', parts, payload=payload):
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
            _, parts, _ = self._route(); payload = self._body()
            if self._manage('DELETE', parts, payload=payload):
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
        result = self.server.management.dispatch(method, parts[2:],
            {key: values[0] for key, values in (query or {}).items()}, payload)
        self._send_json(200, result)
        return True

    def _static(self, name):
        target = (STATIC_ROOT / name).resolve()
        if STATIC_ROOT.resolve() not in target.parents or not target.is_file():
            raise APIError(404, '文件不存在')
        body = target.read_bytes()
        self.send_response(200); self.send_header('Content-Type', mimetypes.guess_type(target.name)[0] or 'application/octet-stream'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


def create_sqlite_server(host='127.0.0.1', port=8878, *, root=None, data_dir=None, schema_dir=None):
    base = Path(root).resolve() if root else Path(__file__).resolve().parents[1]
    manager = SQLiteManager(data_dir or base / 'sqlite_dat', schema_dir or base / 'def' / 'table')
    server = ThreadingHTTPServer((host, port), SQLiteRequestHandler)
    server.sqlite_manager = manager  # type: ignore[attr-defined]
    server.management = SQLiteManagement(manager)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='RTDB SQLite WEB management service')
    parser.add_argument('--host', default='127.0.0.1'); parser.add_argument('--port', type=int, default=8878)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1]); parser.add_argument('--data-dir', type=Path); parser.add_argument('--schema-dir', type=Path)
    args = parser.parse_args(argv)
    server = create_sqlite_server(args.host, args.port, root=args.root, data_dir=args.data_dir, schema_dir=args.schema_dir)
    print(f'RTDB SQLite web service listening on http://{args.host}:{args.port}')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
