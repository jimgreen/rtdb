"""Dependency-free HTTP service for RTDB definitions and rows.

Run from the repository root with ``python -m web.server`` or directly with
``python web/server.py``.  The service uses Python's threaded standard-library
HTTP server, so it does not add a Flask/FastAPI dependency to this small data
store.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import FileMappedDatabase, SchemaError, TableDefinition
from core.file_mapped_db import StorageError


MAX_JSON_BYTES = 16 * 1024 * 1024
STATIC_ROOT = Path(__file__).resolve().parent / "static"


class APIError(Exception):
    def __init__(self, status: int, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def _definition_json(definition: TableDefinition) -> dict[str, Any]:
    return definition.to_mapping()


def _parse_int(value: Optional[str], field: str, *, default: Optional[int] = None, minimum: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        result = int(value)
    except ValueError as exc:
        raise APIError(400, f"{field} must be an integer") from exc
    if minimum is not None and result < minimum:
        raise APIError(400, f"{field} must be at least {minimum}")
    return result


def _path_index(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise APIError(400, "row index must be an integer") from exc


class RTDBWebApp:
    """Application object containing the storage paths and REST handlers."""

    def __init__(
        self,
        root: Optional[os.PathLike[str] | str] = None,
        *,
        data_dir: Optional[os.PathLike[str] | str] = None,
        schema_dir: Optional[os.PathLike[str] | str] = None,
    ) -> None:
        base = Path(root).expanduser().resolve() if root is not None else Path(__file__).resolve().parents[1]
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir is not None else base / "dat"
        self.schema_dir = Path(schema_dir).expanduser().resolve() if schema_dir is not None else base / "def" / "table"
        self.schema_dir.mkdir(parents=True, exist_ok=True)
        self.db = FileMappedDatabase(self.data_dir, schema_directory=self.schema_dir)
        self._definition_lock = threading.RLock()

    def _name(self, name: str) -> str:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise APIError(400, "table name must be an English identifier")
        return name

    def _definition(self, name: str) -> Optional[TableDefinition]:
        name = self._name(name)
        path = self.schema_dir / f"{name}.json"
        if not path.exists():
            return None
        try:
            return TableDefinition.from_json_file(path)
        except SchemaError as exc:
            raise APIError(422, str(exc)) from exc

    def list_definitions(self) -> list[dict[str, Any]]:
        result = []
        for name in sorted(self.db.schema_registry.names() if self.db.schema_registry is not None else ()):
            definition = self._definition(name)
            if definition is not None:
                result.append({
                    "name": name,
                    "row_count": len(self.db.table(name)),
                    "definition": _definition_json(definition),
                })
        return result

    def get_definition(self, name: str) -> dict[str, Any]:
        name = self._name(name)
        definition = self._definition(name)
        if definition is None:
            raise APIError(404, f"table definition not found: {name}")
        return {"name": name, "definition": _definition_json(definition)}

    def save_definition(self, name: str, payload: Mapping[str, Any], *, replace: bool) -> dict[str, Any]:
        name = self._name(name)
        with self._definition_lock:
            existing = self._definition(name)
            if existing is not None and not replace:
                raise APIError(409, f"table definition already exists: {name}")
            try:
                definition = TableDefinition.from_mapping(payload, self.schema_dir, name)
                current = self.db.table(name)
                definition.validate_rows(current.select())
                path = definition.to_json(self.schema_dir, overwrite=True)
            except SchemaError as exc:
                raise APIError(422, str(exc)) from exc
            except (OSError, TypeError, ValueError) as exc:
                raise APIError(400, str(exc)) from exc
            self.db.refresh_schema()
            return {"name": name, "path": str(path), "definition": _definition_json(definition)}

    def delete_definition(self, name: str, *, delete_data: bool = False) -> dict[str, Any]:
        name = self._name(name)
        with self._definition_lock:
            path = self.schema_dir / f"{name}.json"
            if not path.exists():
                raise APIError(404, f"table definition not found: {name}")
            table = self.db.table(name)
            if len(table) and not delete_data:
                raise APIError(409, "table contains data; pass delete_data=true to delete definition and data")
            path.unlink()
            if delete_data:
                table.path.unlink(missing_ok=True)
                table.lock_path.unlink(missing_ok=True)
            self.db.refresh_schema()
            return {"name": name, "deleted": True, "data_deleted": delete_data}

    @staticmethod
    def _where(value: Optional[str]) -> Any:
        if value is None or value == "":
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if not isinstance(parsed, (dict, list, str, int, float, bool)) and parsed is not None:
            raise APIError(400, "where must be a mapping or expression")
        return parsed

    def select_rows(self, name: str, query: Mapping[str, list[str]]) -> dict[str, Any]:
        table = self.db.table(self._name(name))
        where = self._where(query.get("where", [None])[0])
        order_by = query.get("order_by", [None])[0]
        columns_value = query.get("columns", [None])[0]
        columns = columns_value.split(",") if columns_value else None
        limit = _parse_int(query.get("limit", [None])[0], "limit", default=None, minimum=0)
        offset = _parse_int(query.get("offset", [None])[0], "offset", default=0, minimum=0) or 0
        try:
            rows = table.select(where, columns=columns, order_by=order_by, limit=limit, offset=offset)
        except (ValueError, StorageError) as exc:
            raise APIError(400, str(exc)) from exc
        return {"table": table.name, "rows": rows, "count": len(rows), "offset": offset, "limit": limit}

    def insert_rows(self, name: str, payload: Any) -> dict[str, Any]:
        table = self.db.table(self._name(name))
        try:
            if isinstance(payload, Mapping) and isinstance(payload.get("rows"), list):
                count = table.insert_many(payload["rows"])
                return {"table": table.name, "inserted": count}
            if not isinstance(payload, Mapping):
                raise APIError(400, "POST data body must be a row object or {rows: []}")
            row_id = table.insert_one(payload)
            return {"table": table.name, "inserted": 1, "index": row_id, "row": table[row_id]}
        except APIError:
            raise
        except (TypeError, ValueError, SchemaError, StorageError) as exc:
            raise APIError(422, str(exc)) from exc

    def update_rows(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("values"), Mapping):
            raise APIError(400, "PUT data body requires a values object and optional where/limit")
        table = self.db.table(self._name(name))
        try:
            changed = table.update(payload["values"], payload.get("where"), limit=payload.get("limit"))
        except (TypeError, ValueError, SchemaError, StorageError) as exc:
            raise APIError(422, str(exc)) from exc
        return {"table": table.name, "updated": changed}

    def delete_rows(self, name: str, payload: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        payload = payload or {}
        if not isinstance(payload, Mapping):
            raise APIError(400, "DELETE data body must be an object")
        table = self.db.table(self._name(name))
        try:
            removed = table.delete(payload.get("where"), limit=payload.get("limit"))
        except (TypeError, ValueError, SchemaError, StorageError) as exc:
            raise APIError(422, str(exc)) from exc
        return {"table": table.name, "deleted": removed}

    def get_row(self, name: str, index: int) -> dict[str, Any]:
        table = self.db.table(self._name(name))
        try:
            return {"table": table.name, "index": index, "row": table[index]}
        except IndexError as exc:
            raise APIError(404, "row not found") from exc

    def replace_row(self, name: str, index: int, row: Any) -> dict[str, Any]:
        if not isinstance(row, Mapping):
            raise APIError(400, "row body must be an object")
        table = self.db.table(self._name(name))
        try:
            table[index] = row
            return {"table": table.name, "index": index, "row": table[index]}
        except (IndexError, TypeError, ValueError, SchemaError, StorageError) as exc:
            raise APIError(422, str(exc)) from exc

    def delete_row(self, name: str, index: int) -> dict[str, Any]:
        table = self.db.table(self._name(name))
        try:
            row = table.pop(index)
        except IndexError as exc:
            raise APIError(404, "row not found") from exc
        return {"table": table.name, "index": index, "row": row}


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "RTDBWeb/1.0"

    @property
    def app(self) -> RTDBWebApp:
        return self.server.rtdb_app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: Exception) -> None:
        if isinstance(error, APIError):
            self._send_json(error.status, {"error": error.message, "details": error.details})
        else:
            self._send_json(500, {"error": "internal server error", "details": str(error)})

    def _json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_JSON_BYTES:
            raise APIError(413, "request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(400, "request body must be valid UTF-8 JSON") from exc

    def _route(self) -> tuple[str, list[str], Mapping[str, list[str]]]:
        parsed = urlsplit(self.path)
        parts = [unquote(item) for item in parsed.path.split("/") if item]
        return parsed.path, parts, parse_qs(parsed.query, keep_blank_values=True)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            path, parts, query = self._route()
            # Browsers request a favicon automatically.  Treat the optional
            # icon as an empty successful response so a clean management page
            # does not produce a spurious 404 in the browser console.
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/api/health":
                self._send_json(200, {"ok": True, "service": "rtdb"})
                return
            if parts and parts[0] == "api":
                if parts == ["api", "schemas"]:
                    self._send_json(200, {"schemas": self.app.list_definitions()})
                    return
                if len(parts) == 3 and parts[1] == "schemas":
                    self._send_json(200, self.app.get_definition(parts[2]))
                    return
                if len(parts) == 3 and parts[1] == "data":
                    self._send_json(200, self.app.select_rows(parts[2], query))
                    return
                if len(parts) == 4 and parts[1] == "data":
                    self._send_json(200, self.app.get_row(parts[2], _path_index(parts[3])))
                    return
            self._send_static(path)
        except Exception as exc:
            self._send_error(exc)

    def do_POST(self) -> None:
        try:
            _, parts, _ = self._route()
            payload = self._json_body()
            if parts == ["api", "schemas"]:
                name = payload.get("head", {}).get("eng_name") if isinstance(payload, Mapping) else None
                if not isinstance(name, str):
                    raise APIError(400, "head.eng_name is required")
                self._send_json(201, self.app.save_definition(name, payload, replace=False))
                return
            if len(parts) == 3 and parts[1] == "data":
                self._send_json(201, self.app.insert_rows(parts[2], payload))
                return
            raise APIError(404, "route not found")
        except Exception as exc:
            self._send_error(exc)

    def do_PUT(self) -> None:
        try:
            _, parts, _ = self._route()
            payload = self._json_body()
            if len(parts) == 3 and parts[0:2] == ["api", "schemas"]:
                self._send_json(200, self.app.save_definition(parts[2], payload, replace=True))
                return
            if len(parts) == 3 and parts[1] == "data":
                self._send_json(200, self.app.update_rows(parts[2], payload))
                return
            if len(parts) == 4 and parts[1] == "data":
                self._send_json(200, self.app.replace_row(parts[2], _path_index(parts[3]), payload))
                return
            raise APIError(404, "route not found")
        except Exception as exc:
            self._send_error(exc)

    def do_DELETE(self) -> None:
        try:
            _, parts, query = self._route()
            payload = self._json_body() if self.headers.get("Content-Length") else {}
            if len(parts) == 3 and parts[0:2] == ["api", "schemas"]:
                self._send_json(200, self.app.delete_definition(parts[2], delete_data=False))
                return
            if len(parts) == 3 and parts[1] == "data":
                self._send_json(200, self.app.delete_rows(parts[2], payload))
                return
            if len(parts) == 4 and parts[1] == "data":
                self._send_json(200, self.app.delete_row(parts[2], _path_index(parts[3])))
                return
            raise APIError(404, "route not found")
        except Exception as exc:
            self._send_error(exc)

    def _send_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.removeprefix("/")
        if relative == "static" or relative.startswith("static/"):
            relative = relative.removeprefix("static/")
        if ".." in Path(relative).parts:
            raise APIError(404, "file not found")
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in target.parents and target != STATIC_ROOT.resolve():
            raise APIError(404, "file not found")
        if not target.exists() or not target.is_file():
            raise APIError(404, "file not found")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8877,
    *,
    root: Optional[os.PathLike[str] | str] = None,
    data_dir: Optional[os.PathLike[str] | str] = None,
    schema_dir: Optional[os.PathLike[str] | str] = None,
    engine: str = "file_mapped",
) -> ThreadingHTTPServer:
    if engine == "sqlite":
        from .sqlite_server import create_sqlite_server
        return create_sqlite_server(host, port, root=root, data_dir=data_dir, schema_dir=schema_dir)
    if engine != "file_mapped":
        raise ValueError("engine must be file_mapped or sqlite")
    app = RTDBWebApp(root, data_dir=data_dir, schema_dir=schema_dir)
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.rtdb_app = app  # type: ignore[attr-defined]
    return server


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="RTDB table definition and data web service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--schema-dir", type=Path)
    parser.add_argument("--engine", choices=("file_mapped", "sqlite"), default="file_mapped")
    args = parser.parse_args(argv)
    server = create_server(args.host, args.port, root=args.root, data_dir=args.data_dir, schema_dir=args.schema_dir, engine=args.engine)
    print(f"RTDB web service listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

