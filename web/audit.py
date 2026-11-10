"""Durable, date-partitioned operation logs for the SQLite management service."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from core.file_mapped_db import _locked


_RESULT_KEYS = {
    "ok", "name", "mode", "format", "filename", "mime", "count", "total", "table_total",
    "rowcount", "inserted", "updated", "deleted", "imported", "incoming", "before", "after",
    "added", "removed", "changed", "rows", "pending", "tables", "results", "primary_keys",
    "schema_version", "elapsed_ms", "truncated", "warnings", "duplicates", "skipped",
}
_DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_MAX_RETENTION_DAYS = 36500
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 500
_CLEANUP_INTERVAL_HOURS = 24


def _safe(value, *, depth=0):
    """Make a bounded JSON-safe value for an audit record."""
    if depth > 3:
        return "…"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(key): _safe(item, depth=depth + 1) for key, item in list(value.items())[:40]}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item, depth=depth + 1) for item in list(value)[:40]]
    return str(value)


def result_summary(value):
    """Keep operation counts and outcome fields without copying table data."""
    if not isinstance(value, dict):
        return _safe(value)
    summary = {}
    for key, item in value.items():
        if key not in _RESULT_KEYS:
            continue
        if key in {"rows", "results", "tables"} and isinstance(item, list):
            summary[f"{key}_count"] = len(item)
            if key == "results":
                failed = [entry for entry in item if isinstance(entry, dict) and entry.get("ok") is False]
                if failed:
                    summary["failed"] = _safe(failed)
            continue
        if key == "pending" and isinstance(item, list):
            summary[key] = {"count": len(item), "names": _safe(item)}
            continue
        summary[key] = _safe(item)
    return summary


def error_summary(error, status_code):
    details = getattr(error, "details", None)
    result = {"type": type(error).__name__, "message": str(getattr(error, "message", error)),
              "http_status": status_code}
    if details:
        result["details"] = _safe(details)
    return result


def status_for_exception(error):
    status = getattr(error, "status", None)
    if isinstance(status, int):
        return status
    if type(error).__name__ == "SQLiteBusyError":
        return 503
    if type(error).__name__ == "SchemaError":
        return 422
    if isinstance(error, (ValueError, TypeError, OverflowError, OSError)):
        return 400
    return 500


def _local_now():
    return datetime.now().astimezone()


def _record_date(record):
    timestamp = record.get("timestamp") if isinstance(record, dict) else None
    if not isinstance(timestamp, str):
        return ""
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


class OperationAuditLog:
    """Append-only JSONL logs partitioned by local calendar date.

    New records are written to ``rtdb-operation-logs/YYYY-MM-DD.jsonl``. The
    former single JSONL file remains readable so an upgrade does not hide
    existing history. Retention is disabled by default (0 means permanent)
    and can be enabled through the management API.
    """

    def __init__(self, root, *, filename="rtdb-operation-log.jsonl", read_limit=500):
        self.root = Path(root).resolve()
        self.log_dir = self.root / "rtdb-operation-logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_path = self.root / filename
        self.config_path = self.root / "rtdb-operation-log-config.json"
        self.config_lock = self.config_path.with_name(self.config_path.name + ".lock")
        self.read_limit = max(1, int(read_limit))
        self._lock = threading.RLock()
        self._maybe_cleanup()

    @property
    def path(self):
        """Compatibility path for callers that need the current date file."""
        return self._date_path(_local_now().date())

    @staticmethod
    def _validate_date(value):
        if value in (None, "", "all"):
            return None
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise ValueError("日志日期必须为 YYYY-MM-DD")
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("日志日期必须为 YYYY-MM-DD") from error

    @staticmethod
    def _validate_retention(value):
        if isinstance(value, bool):
            raise ValueError("日志保留天数必须是整数")
        try:
            value = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("日志保留天数必须是整数") from error
        if not 0 <= value <= _MAX_RETENTION_DAYS:
            raise ValueError(f"日志保留天数必须在 0–{_MAX_RETENTION_DAYS} 之间")
        return value

    def _date_path(self, value):
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, str):
            value = self._validate_date(value)
        if not isinstance(value, date):
            raise ValueError("无效的日志日期")
        return self.log_dir / f"{value.isoformat()}.jsonl"

    def _default_config(self):
        return {"retention_days": 0, "cleanup_interval_hours": _CLEANUP_INTERVAL_HOURS,
                "last_cleanup_at": None}

    def _read_config(self):
        result = self._default_config()
        if self.config_path.exists():
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                result.update(loaded)
        try:
            result["retention_days"] = self._validate_retention(result.get("retention_days", 0))
        except ValueError:
            result["retention_days"] = 0
        result["cleanup_interval_hours"] = _CLEANUP_INTERVAL_HOURS
        return result

    def config(self):
        try:
            with self._lock, _locked(self.config_lock):
                return self._read_config()
        except Exception:
            return self._default_config()

    def _write_config(self, config):
        temporary = self.config_path.with_name(f".{self.config_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.config_path)

    def set_retention(self, retention_days):
        days = self._validate_retention(retention_days)
        with self._lock, _locked(self.config_lock):
            config = self._read_config()
            config["retention_days"] = days
            config["last_cleanup_at"] = None
            self._write_config(config)
        cleanup = self.cleanup()
        return {**self.config(), **cleanup}

    def _maybe_cleanup(self):
        config = self.config()
        if not config.get("retention_days"):
            return
        marker = config.get("last_cleanup_at")
        if marker:
            try:
                elapsed = (_local_now() - datetime.fromisoformat(marker)).total_seconds()
                if elapsed < _CLEANUP_INTERVAL_HOURS * 3600:
                    return
            except (TypeError, ValueError):
                pass
        try:
            self.cleanup()
        except Exception:
            pass

    def _log_paths(self, selected_date=None):
        selected_date = self._validate_date(selected_date)
        paths = []
        if selected_date is None:
            paths.extend(sorted(self.log_dir.glob("*.jsonl"), reverse=True))
        else:
            path = self._date_path(selected_date)
            if path.is_file():
                paths.append(path)
        if self.legacy_path.is_file():
            paths.append(self.legacy_path)
        return paths

    def available_dates(self):
        values = set()
        for path in self.log_dir.glob("*.jsonl"):
            match = _DATE_FILE.match(path.name)
            if match:
                try:
                    date.fromisoformat(match.group(1))
                except ValueError:
                    continue
                values.add(match.group(1))
        if self.legacy_path.is_file():
            try:
                with self.legacy_path.open("r", encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        try:
                            current = _record_date(json.loads(line))
                        except (json.JSONDecodeError, TypeError):
                            current = ""
                        if current:
                            values.add(current)
            except OSError:
                pass
        return sorted(values, reverse=True)

    @staticmethod
    def _matches(record, *, category=None, status=None, keyword=None, table=None, operation=None):
        if category and record.get("category") != category:
            return False
        if status and record.get("status") != status:
            return False
        if table and str(record.get("table") or "") != table:
            return False
        if operation and operation.casefold() not in str(record.get("operation") or "").casefold():
            return False
        if keyword:
            haystack = json.dumps(record, ensure_ascii=False, separators=(",", ":")).casefold()
            if keyword.casefold() not in haystack:
                return False
        return True

    def query(self, *, date_value=None, page=1, page_size=None, category=None, status=None,
              keyword=None, table=None, operation=None):
        selected_date = self._validate_date(date_value)
        try:
            page = int(page)
            page_size = _DEFAULT_PAGE_SIZE if page_size is None else int(page_size)
        except (TypeError, ValueError) as error:
            raise ValueError("日志页码和分页大小必须是整数") from error
        if page < 1 or not 1 <= page_size <= _MAX_PAGE_SIZE:
            raise ValueError(f"日志页码必须大于 0，分页大小必须在 1–{_MAX_PAGE_SIZE} 之间")
        start, end = (page - 1) * page_size, page * page_size
        records, total = [], 0
        for path in self._log_paths(selected_date):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if selected_date is not None and _record_date(record) != selected_date.isoformat():
                    continue
                if not self._matches(record, category=category, status=status, keyword=keyword,
                                     table=table, operation=operation):
                    continue
                if start <= total < end:
                    records.append(record)
                total += 1
        return {"logs": records, "count": len(records), "total": total, "page": page,
                "page_size": page_size, "pages": max(1, (total + page_size - 1) // page_size),
                "date": selected_date.isoformat() if selected_date else "", "dates": self.available_dates()}

    def _count_lines(self, path):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                return sum(1 for _ in stream)
        except OSError:
            return 0

    def cleanup(self, *, retention_days=None):
        config = self.config()
        days = config["retention_days"] if retention_days is None else self._validate_retention(retention_days)
        deleted_files, deleted_records = [], 0
        cutoff = None
        if days > 0:
            cutoff = _local_now().date() - timedelta(days=days - 1)
            with self._lock:
                for path in sorted(self.log_dir.glob("*.jsonl")):
                    match = _DATE_FILE.match(path.name)
                    if not match:
                        continue
                    try:
                        log_date = date.fromisoformat(match.group(1))
                    except ValueError:
                        continue
                    if log_date < cutoff:
                        with _locked(path.with_name(path.name + ".lock")):
                            deleted_records += self._count_lines(path)
                            path.unlink(missing_ok=True)
                            deleted_files.append(path.name)
                if self.legacy_path.is_file():
                    retained, removed = [], 0
                    try:
                        for line in self.legacy_path.read_text(encoding="utf-8", errors="replace").splitlines(True):
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                retained.append(line)
                                continue
                            if _record_date(record) and _record_date(record) < cutoff.isoformat():
                                removed += 1
                            else:
                                retained.append(line)
                    except OSError:
                        retained = []
                    if removed:
                        temporary = self.legacy_path.with_name(f".{self.legacy_path.name}.{uuid.uuid4().hex}.tmp")
                        temporary.write_text("".join(retained), encoding="utf-8")
                        os.replace(temporary, self.legacy_path)
                        deleted_records += removed
                        if not retained:
                            self.legacy_path.unlink(missing_ok=True)
                            deleted_files.append(self.legacy_path.name)
        now = _local_now().isoformat(timespec="seconds")
        with self._lock, _locked(self.config_lock):
            config = self._read_config()
            config["last_cleanup_at"] = now
            self._write_config(config)
        return {"retention_days": days, "cutoff_date": cutoff.isoformat() if cutoff else None,
                "deleted_files": deleted_files, "deleted_records": deleted_records,
                "dates": self.available_dates()}

    def append(self, *, category, operation, status, http_status, method, path,
               table=None, duration_ms=0, result=None, error=None):
        self._maybe_cleanup()
        record = {
            "id": uuid.uuid4().hex,
            "timestamp": _local_now().isoformat(timespec="milliseconds"),
            "category": category,
            "operation": operation,
            "status": status,
            "http_status": http_status,
            "method": method,
            "path": path,
            "table": table,
            "duration_ms": round(float(duration_ms), 2),
            "result": result_summary(result) if result is not None else None,
            "error": _safe(error),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        try:
            target = self.path
            with self._lock, _locked(target.with_name(target.name + ".lock")):
                with target.open("a", encoding="utf-8", newline="") as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
        except Exception:
            return None
        return record

    def read(self, limit=None):
        """Compatibility reader returning newest records first."""
        size = self.read_limit if limit is None else max(1, min(int(limit), 5000))
        return self.query(page=1, page_size=size)["logs"]
