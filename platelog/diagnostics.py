"""Bounded, allowlisted diagnostics and an offline support ZIP (standard library only)."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.metadata
import io
import json
import logging
import os
import platform
import re
import secrets
import shutil
import sqlite3
import ssl
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

DAY_MS = 86_400_000
HISTORY_DAYS = 30
FILE_BYTES = 2 * 1024 * 1024
FILE_COUNT = 4
MAX_EVENT_BYTES = 4096
MAX_ACTIVITY = 5000
MAX_BUNDLE_INPUT = 12 * 1024 * 1024
RANGES = {"1": "Last 24 hours", "2": "Last 2 days", "3": "Last 3 days", "all": "All available history"}
COMPONENTS = {"service", "web", "camera", "admin", "retention", "csv", "registered_list", "alarms"}
EVENTS = {"service.launch", "service.ready", "service.stopped", "service.failed", "listener.unavailable", "listener.restored",
          "request.failed", "request.rejected", "camera.accepted", "camera.rejected", "camera.storage_failed", "camera.auth_failed",
          "retention.completed", "retention.failed", "csv.completed", "csv.failed", "registration.refresh_completed",
          "registration.refresh_failed", "registration.import_completed", "registration.import_failed", "alarm.delivery",
          "alarm.failed", "smtp.test", "admin.folder_test", "support.download", "excel.completed"}
OUTCOMES = {"accepted", "partial", "failed", "unconfirmed", "logged", "held", "pending", "dispatching",
            "registered", "unregistered", "off", "written", "exists", "error", "success"}
ENUM_FIELDS = {"outcome": OUTCOMES, "channel": {"vapix", "smtp", "both"},
               "security": {"none", "starttls", "tls"}, "mode": {"preview", "pilot", "simulation"},
               "route": {"admin", "camera", "photo", "excel", "plate_log", "health", "other"},
               "method": {"GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS"}}
NUMBER_FIELDS = {"status", "bytes", "photos", "passages", "packets", "added", "removed", "total",
                 "skipped", "staff_port", "camera_port", "device_error_code", "days", "exit_code"}
BOOL_FIELDS = {"duplicate", "https", "enabled", "camera_online"}
_sinks: dict[Path, DiagnosticSink] = {}
_sinks_lock = threading.Lock()
DOWNLOAD_LOCK = threading.Lock()
APP = Path(__file__).resolve().parent


def utc(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds")


def _safe_fields(fields: dict) -> dict:
    safe = {}
    for name, value in fields.items():
        if name in NUMBER_FIELDS and type(value) is int and 0 <= value < 2**63:
            safe[name] = value
        elif name == "capture_age_ms" and type(value) is int and abs(value) < 2**63:
            safe[name] = value
        elif name in BOOL_FIELDS and type(value) is bool:
            safe[name] = value
        elif name in ENUM_FIELDS and isinstance(value, str) and value in ENUM_FIELDS[name]:
            safe[name] = value
        elif name == "camera_ref" and isinstance(value, str) and re.fullmatch(r"camera-[0-9a-f]{12}", value):
            safe[name] = value
    return safe


def exception_details(error: BaseException) -> list[dict]:
    """Keep types, error numbers and app stack locations; never messages, arguments or locals."""
    result, pending, seen = [], [error], set()
    while pending and len(result) < 5:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        item = {"type": type(current).__name__[:80], "frames": []}
        for name in ("errno", "winerror", "sqlite_errorcode", "smtp_code", "verify_code"):
            value = getattr(current, name, None)
            if type(value) is int:
                item[name] = value
        for name in ("sqlite_errorname", "reason"):
            value = getattr(current, name, None)
            if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value):
                item[name] = value
        tb = current.__traceback__
        while tb:
            code = tb.tb_frame.f_code
            filename = Path(code.co_filename)
            if filename.parent.resolve() == APP:
                item["frames"].append({"file": filename.name, "function": code.co_name[:80], "line": tb.tb_lineno})
            tb = tb.tb_next
        item["frames"] = item["frames"][-12:]
        result.append(item)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions[:3])
    return result


class DiagnosticSink:
    def __init__(self, directory: Path):
        self.directory = directory / "diagnostics"
        self.lock = threading.RLock()
        self.last_prune_ms = 0
        self.last_write_error: str | None = None
        self._key: bytes | None = None

    def paths(self) -> list[Path]:
        return [self.directory / name for name in ("events.jsonl", "events.jsonl.1", "events.jsonl.2", "events.jsonl.3")]

    def camera_ref(self, camera: str) -> str | None:
        try:
            with self.lock:
                if self._key is None:
                    key_path = self.directory / "identity.key"
                    if self.directory.is_symlink() or self.directory.is_junction():
                        return None
                    self.directory.mkdir(parents=True, exist_ok=True)
                    if key_path.is_symlink() or key_path.is_junction():
                        return None
                    try:
                        with key_path.open("xb") as stream:
                            stream.write(secrets.token_bytes(32))
                    except FileExistsError:
                        pass
                    with key_path.open("rb") as stream:
                        self._key = stream.read(33)
                    if len(self._key) != 32:
                        self._key = None
                        return None
                return "camera-" + hmac.digest(self._key, camera.encode(), "sha256").hex()[:12]
        except OSError:
            return None

    def _prune(self, now_ms: int) -> None:
        if now_ms - self.last_prune_ms < DAY_MS:
            return
        lower = now_ms - HISTORY_DAYS * DAY_MS
        for path in self.paths():
            if not path.exists() or path.is_symlink() or path.is_junction():
                continue
            retained = []
            with path.open("rb") as source:
                data = source.read(FILE_BYTES)
            for line in data.splitlines(keepends=True):
                try:
                    if json.loads(line).get("at_ms", 0) >= lower:
                        retained.append(line)
                except (ValueError, TypeError, AttributeError):
                    pass
            temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
            with temporary.open("xb") as stream:
                stream.write(b"".join(retained))
            temporary.replace(path)
        self.last_prune_ms = now_ms

    def record(self, event: str, component: str, *, level: str = "info", error: BaseException | None = None,
               camera: str | None = None, now_ms: int | None = None, **fields) -> bool:
        if component not in COMPONENTS or level not in {"info", "warning", "error"} or event not in EVENTS:
            return False
        now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        entry = {"at_ms": now_ms, "level": level, "component": component, "event": event,
                 "data": _safe_fields(fields)}
        if camera:
            if ref := self.camera_ref(camera):
                entry["data"]["camera_ref"] = ref
        if error is not None:
            entry["errors"] = exception_details(error)
        data = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_EVENT_BYTES:
            entry["errors"] = [{"type": type(error).__name__[:80]}] if error else []
            entry["stack_truncated"] = True
            data = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
        try:
            with self.lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                if self.directory.is_symlink() or self.directory.is_junction():
                    return False
                paths = self.paths()
                if any(path.is_symlink() or path.is_junction() for path in paths):
                    return False
                self._prune(now_ms)
                if paths[0].exists() and paths[0].stat().st_size + len(data) > FILE_BYTES:
                    paths[-1].unlink(missing_ok=True)
                    for index in range(FILE_COUNT - 2, -1, -1):
                        if paths[index].exists():
                            paths[index].replace(paths[index + 1])
                with paths[0].open("ab") as stream:
                    stream.write(data)
                self.last_write_error = None
            return True
        except OSError as exc:
            self.last_write_error = type(exc).__name__
            return False


def sink(directory: Path) -> DiagnosticSink:
    key = directory.resolve()
    with _sinks_lock:
        if key not in _sinks:
            _sinks[key] = DiagnosticSink(key)
        return _sinks[key]


def record_event(directory: Path, event: str, component: str, **fields) -> bool:
    try:
        return sink(directory).record(event, component, **fields)
    except Exception:
        # Diagnostics must not turn a successful passage or background job into a failure.
        return False


def route_category(path: str) -> str:
    for prefix, label in (("/admin", "admin"), ("/api/axis/", "camera"), ("/images/", "photo"),
                          ("/register/export", "excel"), ("/healthz", "health"), ("/register/", "plate_log")):
        if path.startswith(prefix):
            return label
    return "plate_log" if path == "/" else "other"


class SafeServerErrorHandler(logging.Handler):
    """Capture server errors without copying formatted logging messages or request arguments."""
    def __init__(self, directory: Path):
        super().__init__(logging.ERROR)
        self.directory = directory

    def emit(self, record: logging.LogRecord) -> None:
        error = record.exc_info[1] if record.exc_info else None
        record_event(self.directory, "request.failed", "web", level="error", error=error)


def attach_server_error_logging(directory: Path) -> None:
    logger = logging.getLogger("uvicorn.error")
    for handler in logger.handlers[:]:
        if isinstance(handler, SafeServerErrorHandler):
            logger.removeHandler(handler)
    logger.addHandler(SafeServerErrorHandler(directory))


def _read_events(directory: Path, days: str, now_ms: int) -> tuple[list[dict], dict]:
    selected, bad, limited, total_bytes = [], 0, False, 0
    lower = now_ms - int(days) * DAY_MS if days != "all" else now_ms - HISTORY_DAYS * DAY_MS
    logger = sink(directory)
    app_filenames = {path.name for path in APP.glob("*.py")}
    if logger.directory.is_symlink() or logger.directory.is_junction():
        return [], {"records": 0, "input_limited": True, "malformed_or_unreadable": 1, "last_write_error_type": "LinkedDirectory"}
    with logger.lock:
        for path in reversed(logger.paths()):
            if not path.exists():
                continue
            if path.is_symlink() or path.is_junction():
                limited = True
                continue
            try:
                with path.open("rb") as stream:
                    data = stream.read(FILE_BYTES + 1)
                limited |= len(data) > FILE_BYTES
                total_bytes += min(len(data), FILE_BYTES)
                for line in data[:FILE_BYTES].splitlines():
                    if len(line) > MAX_EVENT_BYTES:
                        bad += 1
                        limited = True
                        continue
                    try:
                        raw = json.loads(line)
                        if (type(raw.get("at_ms")) is not int or raw.get("component") not in COMPONENTS
                                or raw.get("level") not in {"info", "warning", "error"}
                                or raw.get("event") not in EVENTS):
                            raise ValueError
                        if raw["at_ms"] < lower:
                            continue
                        entry = {key: raw[key] for key in ("at_ms", "level", "component", "event")}
                        entry["at_utc"] = utc(raw["at_ms"])
                        entry["data"] = _safe_fields(raw.get("data", {}))
                        if raw.get("stack_truncated") is True:
                            entry["stack_truncated"] = True
                        errors = []
                        for error in raw.get("errors", [])[:5]:
                            name = error.get("type", "")
                            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name):
                                continue
                            clean = {"type": name, "frames": []}
                            for key in ("errno", "winerror", "sqlite_errorcode", "smtp_code", "verify_code"):
                                if type(error.get(key)) is int:
                                    clean[key] = error[key]
                            for key in ("sqlite_errorname", "reason"):
                                if isinstance(error.get(key), str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error[key]):
                                    clean[key] = error[key]
                            for frame in error.get("frames", [])[-12:]:
                                if (frame.get("file") in app_filenames
                                        and re.fullmatch(r"[A-Za-z_<>][A-Za-z0-9_<>]{0,79}", frame.get("function", ""))
                                        and type(frame.get("line")) is int):
                                    clean["frames"].append({key: frame[key] for key in ("file", "function", "line")})
                            errors.append(clean)
                        if errors:
                            entry["errors"] = errors
                        selected.append(entry)
                    except (ValueError, TypeError, AttributeError, OverflowError, OSError, RecursionError):
                        bad += 1
            except OSError:
                bad += 1
    selected.sort(key=lambda row: row["at_ms"])
    return selected, {"records": len(selected), "malformed_or_unreadable": bad, "input_limited": limited,
                      "stored_bytes_read": total_bytes, "earliest_utc": utc(selected[0]["at_ms"]) if selected else None,
                      "latest_utc": utc(selected[-1]["at_ms"]) if selected else None,
                      "future_dated_records": sum(row["at_ms"] > now_ms + 5000 for row in selected),
                      "last_write_error_type": logger.last_write_error}


def _activity_code(text: str) -> str:
    for prefix, code in (("Camera ", "camera.settings_changed"), ("Registered list", "registration.import"),
                         ("Scheduled registered", "registration.refresh"), ("Daily CSV", "csv.job"),
                         ("Administrator password", "admin.password_changed"), ("Manual purge", "retention.manual"),
                         ("Retention", "retention.cleanup"), ("Alarm rule", "alarms.settings_changed"),
                         ("SMTP", "smtp.settings_changed"), ("Diagnostic", "diagnostics.download")):
        if text.startswith(prefix):
            return code
    return "admin.activity"


def _snapshot(directory: Path, days: str, now_ms: int) -> tuple[dict, dict, dict[str, list]]:
    database = directory / "platelog.sqlite3"
    health = {"database": "missing", "counts": {}, "database_bytes": {}, "activity_limited": False}
    config, activity = {}, {"admin_activity": [], "alarm_activity": [], "import_activity": []}
    for suffix, label in (("", "main"), ("-wal", "wal"), ("-shm", "shm")):
        try:
            health["database_bytes"][label] = Path(str(database) + suffix).stat().st_size
        except OSError:
            health["database_bytes"][label] = None
    if not database.is_file():
        return health, config, activity
    lower = now_ms - int(days) * DAY_MS if days != "all" else 0
    started = time.monotonic()
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)) as db:
            db.row_factory = sqlite3.Row
            db.set_progress_handler(lambda: int(time.monotonic() - started > 3), 10000)
            db.execute("BEGIN")
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            health["database"] = "readable"
            for table in ("passages", "packets", "images", "registered_plates"):
                if table in names:
                    health["counts"][table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if "packets" in names:
                last = db.execute("SELECT received_ms,capture_ms FROM packets ORDER BY rowid DESC LIMIT 1").fetchone()
                if last:
                    health["last_read"] = {"received_ms": last["received_ms"], "capture_ms": last["capture_ms"],
                                           "capture_age_at_receipt_ms": last["received_ms"] - last["capture_ms"]}
            saved = {}
            if "settings" in names:
                keys = ("retention", "daily_csv_schedule", "daily_csv_status", "registered_refresh_schedule",
                        "registered_refresh_status", "smtp_settings", "display_timezone", "unregistered_history_days",
                        "audible_alerts", "web_port", "camera_port", "retention_last_run")
                for name, value in db.execute(f"SELECT name,value FROM settings WHERE name IN ({','.join('?' for _ in keys)})", keys):
                    try:
                        saved[name] = json.loads(value)
                    except ValueError:
                        saved[name] = value
                try:
                    access = db.execute("SELECT json_extract(value,'$.required') FROM settings WHERE name='register_access'").fetchone()
                    config["staff_sign_in_required"] = bool(access[0]) if access else False
                except sqlite3.Error:
                    config["staff_sign_in_required"] = "unavailable"
            retention = saved.get("retention", {})
            config["retention"] = {key: retention[key] for key in ("enabled", "photo_days", "passage_days")
                                   if isinstance(retention, dict) and type(retention.get(key)) in {bool, int}}
            zone = saved.get("display_timezone", "UTC")
            if isinstance(zone, str) and re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+){0,3}", zone):
                config["display_timezone"] = zone
            for name in ("web_port", "camera_port", "unregistered_history_days", "retention_last_run"):
                value = saved.get(name)
                if type(value) is int or (isinstance(value, str) and value.isdecimal()):
                    config[name] = int(value)
            for name, path_key in (("daily_csv_schedule", "folder"), ("registered_refresh_schedule", "source")):
                value = saved.get(name, {})
                config[name] = {"enabled": bool(value.get("enabled")), "path_configured": bool(value.get(path_key))} if isinstance(value, dict) else {}
                clock = value.get("run_time") if isinstance(value, dict) else None
                if isinstance(clock, str) and re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", clock):
                    config[name]["run_time"] = clock
            smtp = saved.get("smtp_settings", {})
            if isinstance(smtp, dict):
                config["smtp"] = {"host_configured": bool(smtp.get("host")), "sender_configured": bool(smtp.get("sender")),
                                  "authentication_configured": bool(smtp.get("username") and smtp.get("secret_name")),
                                  **_safe_fields({"security": smtp.get("security")})}
                if type(smtp.get("port")) is int:
                    config["smtp"]["port"] = smtp["port"]
            csv_status = saved.get("daily_csv_status", {})
            config["latest_csv_result"] = csv_status.get("result") if isinstance(csv_status, dict) and csv_status.get("result") in OUTCOMES else None
            refresh = saved.get("registered_refresh_status", {})
            attempt = refresh.get("last_attempt", {}) if isinstance(refresh, dict) else {}
            config["latest_refresh"] = {"attempt_recorded": bool(attempt), "failed": bool(attempt.get("error"))} if isinstance(attempt, dict) else {}
            if isinstance(attempt, dict):
                config["latest_refresh"].update({key: attempt[key] for key in ("added", "removed", "total") if type(attempt.get(key)) is int})
            sounds = saved.get("audible_alerts", {})
            config["audible_alerts"] = {key: sounds[key] for key in ("registered", "unregistered")
                                        if isinstance(sounds, dict) and type(sounds.get(key)) is bool}
            config["cameras"] = []
            if "camera_settings" in names:
                for row in db.execute("SELECT id,mode,(length(username)>0 AND length(verifier)>0) AS credential_set FROM camera_settings ORDER BY id LIMIT 1000"):
                    config["cameras"].append({"camera_ref": sink(directory).camera_ref(row["id"]),
                        "authentication": row["mode"] if row["mode"] in {"disabled", "none", "basic"} else "unknown",
                        "credential_configured": bool(row["credential_set"])})
            config["alarm_rules"] = []
            if "alarm_rules" in names:
                columns = {row[1] for row in db.execute("PRAGMA table_info(alarm_rules)")}
                allowed = ("camera", "mode", "plate_status", "action", "virtual_input", "pulse_seconds")
                selected = [column for column in allowed if column in columns]
                for row in db.execute(f"SELECT {','.join(selected)} FROM alarm_rules LIMIT 1000"):
                    rule = {"camera_ref": sink(directory).camera_ref(row["camera"])}
                    for key, values in (("mode", {"off", "log", "live"}), ("plate_status", {"registered", "unregistered"}),
                                        ("action", {"vapix", "smtp", "both"})):
                        if key in selected and row[key] in values:
                            rule[key] = row[key]
                    for key in ("virtual_input", "pulse_seconds"):
                        if key in selected and type(row[key]) is int:
                            rule[key] = row[key]
                    config["alarm_rules"].append(rule)
            if "admin_audit" in names:
                rows = db.execute("SELECT at_ms,activity FROM admin_audit WHERE at_ms>=? ORDER BY at_ms DESC LIMIT ?", (lower, MAX_ACTIVITY + 1)).fetchall()
                health["activity_limited"] |= len(rows) > MAX_ACTIVITY
                activity["admin_activity"] = [{"at_ms": row["at_ms"], "event": _activity_code(row["activity"])} for row in rows[:MAX_ACTIVITY]]
            if "alarm_runs" in names:
                rows = db.execute("SELECT result_ms,result FROM alarm_runs WHERE result_ms>=? ORDER BY result_ms DESC LIMIT ?", (lower, MAX_ACTIVITY + 1)).fetchall()
                health["activity_limited"] |= len(rows) > MAX_ACTIVITY
                activity["alarm_activity"] = [{"at_ms": row["result_ms"], "outcome": row["result"] if row["result"] in OUTCOMES else "unknown"} for row in rows[:MAX_ACTIVITY]]
            if "registration_import_runs" in names:
                rows = db.execute("SELECT at_ms,kind,mode,(length(error)>0) AS failed FROM registration_import_runs WHERE at_ms>=? ORDER BY at_ms DESC LIMIT ?", (lower, MAX_ACTIVITY + 1)).fetchall()
                health["activity_limited"] |= len(rows) > MAX_ACTIVITY
                activity["import_activity"] = [{"at_ms": row["at_ms"], "kind": row["kind"] if row["kind"] in {"manual", "scheduled"} else "unknown",
                    "mode": row["mode"] if row["mode"] in {"add", "replace"} else "unknown", "failed": bool(row["failed"])} for row in rows[:MAX_ACTIVITY]]
    except sqlite3.Error as exc:
        health["database"] = "error"
        health["database_error"] = exception_details(exc)
    return health, config, activity


def build_support_bundle(directory: Path, days: str = "1", *, now_ms: int | None = None,
                         runtime: dict | None = None) -> bytes:
    if days not in RANGES:
        raise ValueError("Choose 1 day, 2 days, 3 days or all available history.")
    now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    records, log_health = _read_events(directory, days, now_ms)
    health, config, activities = _snapshot(directory, days, now_ms)
    system = {"generated_utc": utc(now_ms), "python": platform.python_version(), "implementation": platform.python_implementation(),
              "sqlite": sqlite3.sqlite_version, "openssl": ssl.OPENSSL_VERSION,
              "os": {"system": platform.system(), "release": platform.release(), "version": platform.version(), "architecture": platform.machine()},
              "dependencies": {}, "source_sha256": {}}
    for package in ("fastapi", "uvicorn", "Pillow", "tzdata", "starlette", "pydantic", "pydantic_core", "h11"):
        try:
            system["dependencies"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            system["dependencies"][package] = "not installed"
    for path in sorted(APP.glob("*.py")):
        try:
            system["source_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            system["source_sha256"][path.name] = "unreadable"
    system["app_fingerprint"] = hashlib.sha256(json.dumps(system["source_sha256"], sort_keys=True).encode()).hexdigest()
    try:
        usage = shutil.disk_usage(directory)
        health["data_volume"] = {"total_bytes": usage.total, "free_bytes": usage.free}
    except OSError as exc:
        health["data_volume"] = {"error_type": type(exc).__name__}
    config["runtime"] = _safe_fields(runtime or {})
    health["diagnostic_logging"] = log_health
    manifest = {"format_version": 1, "generated_utc": utc(now_ms), "selection": days, "selection_label": RANGES[days],
                "log_limits": {"maximum_days": HISTORY_DAYS, "maximum_bytes": FILE_BYTES * FILE_COUNT},
                "activity_limit_per_file": MAX_ACTIVITY, "new_logs_start_with_this_version": True,
                "privacy": {"raw_payloads": False, "plates": False, "photos": False, "passwords_and_verifiers": False,
                            "addresses_and_usernames": False, "file_paths": False, "environment_values": False},
                "files": {}}
    files = {"system.json": json.dumps(system, indent=2).encode(), "configuration.json": json.dumps(config, indent=2).encode(),
             "health.json": json.dumps(health, indent=2).encode(),
             "events.jsonl": "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in records).encode()}
    files.update({name + ".jsonl": "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows).encode()
                  for name, rows in activities.items()})
    files["README.txt"] = ("PlateLog offline support bundle\n\n"
        "Use manifest.json for the selected period, storage limits and file hashes. system.json identifies the exact source and runtime.\n"
        "events.jsonl contains structured operational events, error types/numbers and app stack locations.\n"
        "Exception messages, source lines, local variables, requests, vehicle data, credentials, addresses and paths are excluded.\n"
        "Camera identifiers are opaque references. The identifier key is not included.\n"
        "Activity files contain redacted retained Admin, alarm and import outcomes. They do not contain original free text.\n"
        "New detailed logs begin with this version; earlier faults cannot be reconstructed. All means available retained history, not unlimited history.\n"
        "Check health.json for truncated/unreadable input, clock anomalies and logging failures. Database counters and configuration are current snapshots.\n"
        "A working bundle does not prove camera, SMTP, relay, DNS, firewall or certificate trust is configured correctly.\n"
        "No data was uploaded and no camera, relay or mail server was contacted to create this bundle.\n").encode()
    if sum(map(len, files.values())) > MAX_BUNDLE_INPUT:
        raise ValueError("The support bundle exceeded its safety limit. Choose a shorter period.")
    manifest["files"] = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()}
    files["manifest.json"] = json.dumps(manifest, indent=2).encode()
    target = io.BytesIO()
    with ZipFile(target, "w", ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return target.getvalue()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create a redacted PlateLog support ZIP without starting the service.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--days", choices=RANGES, default="1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = build_support_bundle(args.data_dir, args.days)
        with args.output.open("xb") as stream:
            stream.write(data)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"PlateLog diagnostics: {type(exc).__name__}. Check the data and output locations; existing files are not overwritten.\n")
    print(f"PlateLog support bundle saved: {args.output}")


if __name__ == "__main__":
    main()
