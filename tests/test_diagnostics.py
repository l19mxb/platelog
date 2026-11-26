import io
import asyncio
import json
import logging
import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlsplit
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from platelog import diagnostics
from platelog.credentials import password_verifier
from platelog.store import Store
from platelog.preview import make_preview, set_admin_password

NOW = 1_800_000_000_000
DAY = diagnostics.DAY_MS


def files(data):
    with ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.store = Store(self.data)

    def records(self, days="1"):
        archive = files(diagnostics.build_support_bundle(self.data, days, now_ms=NOW))
        return [json.loads(line) for line in archive["events.jsonl"].splitlines()]

    def test_periods_are_rolling_and_all_uses_retained_history(self):
        logger = diagnostics.sink(self.data)
        for age in (5 * DAY, 71 * 3_600_000, 47 * 3_600_000, DAY, 1_000):
            logger.record("camera.accepted", "camera", now_ms=NOW - age)
        self.assertEqual([len(self.records(days)) for days in ("1", "2", "3", "all")], [2, 3, 4, 5])
        for bad in ("0", "4", "ALL", "", "../events.jsonl"):
            with self.subTest(days=bad), self.assertRaises(ValueError):
                diagnostics.build_support_bundle(self.data, bad, now_ms=NOW)

    def test_private_values_payloads_paths_and_exception_arguments_are_excluded(self):
        secret = "CANARY_PASSWORD_DO_NOT_SHARE_3928"
        camera = "private-door-location"
        address = "private-contact@example.internal"
        private_path = "C:/Private/CANARY_PRIVATE_IMPORT.csv"
        plate = "PRIVPLATE91"
        self.store.set_setting("admin_password", password_verifier(secret), "Administrator password changed")
        self.store.save_camera(camera, "basic", "CANARY_USERNAME", secret)
        draft = self.store.registration_import({plate.lower(): plate}, True)
        self.store.registration_import({plate.lower(): plate}, True, draft["revision"])
        self.store.set_setting("smtp_settings", json.dumps({"host": "CANARY_MAIL_HOST", "port": 587, "security": "starttls",
            "sender": address, "username": "CANARY_USERNAME", "secret_name": "PLATELOG_SMTP_PASSWORD_CANARY"}), "SMTP settings saved")
        self.store.set_setting("daily_csv_schedule", json.dumps({"enabled": True, "folder": private_path, "run_time": "00:05"}),
                               f"Untrusted free text {secret} {plate} {address}")
        self.store.images.joinpath("not-exported.jpg").write_bytes(b"CANARY_JPEG_BYTES")
        try:
            raise PermissionError(13, secret, private_path)
        except PermissionError as error:
            diagnostics.record_event(self.data, "csv.failed", "csv", now_ms=NOW, level="error", error=error, camera=camera,
                                     payload=secret, username="CANARY_USERNAME", password=secret, filename=private_path)
        archive = files(diagnostics.build_support_bundle(self.data, "all", now_ms=NOW))
        combined = b"\n".join(archive.values())
        for value in (secret, camera, address, private_path, plate, "CANARY_USERNAME", "CANARY_MAIL_HOST",
                      "CANARY_JPEG_BYTES", "PLATELOG_SMTP_PASSWORD_CANARY"):
            with self.subTest(value=value):
                self.assertNotIn(value.encode(), combined)
        rows = [json.loads(line) for line in archive["events.jsonl"].splitlines()]
        self.assertEqual(rows[0]["errors"][0]["type"], "PermissionError")
        self.assertEqual(rows[0]["errors"][0]["errno"], 13)
        self.assertRegex(rows[0]["data"]["camera_ref"], r"^camera-[0-9a-f]{12}$")
        self.assertFalse(any(name.endswith((".jpg", ".key", ".sqlite3")) for name in archive))

    def test_rotation_caps_storage_and_retains_recent_records(self):
        with patch.object(diagnostics, "FILE_BYTES", 1024):
            logger = diagnostics.sink(self.data)
            for index in range(100):
                self.assertTrue(logger.record("camera.accepted", "camera", now_ms=NOW + index, bytes=index))
            self.assertLessEqual(sum(path.stat().st_size for path in logger.paths() if path.exists()), 4 * 1024)
            result = self.records("all")
            self.assertLess(len(result), 100)
            self.assertEqual(result[-1]["data"]["bytes"], 99)

    def test_expired_logs_are_pruned_on_next_write(self):
        logger = diagnostics.sink(self.data)
        logger.record("service.launch", "service", now_ms=NOW - 31 * DAY)
        logger.record("service.ready", "service", now_ms=NOW)
        raw = b"".join(path.read_bytes() for path in logger.paths() if path.exists())
        self.assertNotIn(b'service.launch', raw)
        self.assertEqual([row["event"] for row in self.records("all")], ["service.ready"])

    def test_audit_period_filters_and_redacts_original_free_text(self):
        with closing(self.store.connect()) as db, db:
            db.executemany("INSERT INTO admin_audit(at_ms,activity) VALUES (?,?)", (
                (NOW - 5 * DAY, "Camera PRIVATE_LOCATION configured: CANARY_AUDIT_SECRET"),
                (NOW - 1000, "Daily CSV CANARY_REPORT_SHARE")))
        recent = files(diagnostics.build_support_bundle(self.data, "1", now_ms=NOW))
        full = files(diagnostics.build_support_bundle(self.data, "all", now_ms=NOW))
        self.assertEqual(len(recent["admin_activity.jsonl"].splitlines()), 1)
        self.assertEqual(len(full["admin_activity.jsonl"].splitlines()), 2)
        self.assertNotIn(b"CANARY", b"".join(full.values()))
        self.assertNotIn(b"PRIVATE_LOCATION", b"".join(full.values()))

    def test_missing_and_corrupt_database_still_produce_a_support_bundle(self):
        absent = self.data / "missing-install"
        first = files(diagnostics.build_support_bundle(absent, "all", now_ms=NOW))
        self.assertEqual(json.loads(first["health.json"])["database"], "missing")
        self.assertFalse(absent.exists())
        damaged = self.data / "corrupt"
        damaged.mkdir()
        (damaged / "platelog.sqlite3").write_bytes(b"CANARY_CORRUPTED_DATABASE" * 8)
        second = files(diagnostics.build_support_bundle(damaged, "1", now_ms=NOW))
        health = json.loads(second["health.json"])
        self.assertEqual(health["database"], "error")
        self.assertIn("sqlite_errorname", health["database_error"][0])
        self.assertNotIn(b"CANARY_CORRUPTED_DATABASE", b"".join(second.values()))

    def test_logging_permission_errors_do_not_escape_to_the_application(self):
        logger = diagnostics.sink(self.data)
        with patch("platelog.diagnostics.Path.mkdir", side_effect=PermissionError(13, "CANARY_PRIVATE_PATH")):
            self.assertFalse(logger.record("service.launch", "service"))
        self.assertEqual(logger.last_write_error, "PermissionError")
        self.assertTrue(logger.record("service.ready", "service"))
        self.assertIsNone(logger.last_write_error)

    def test_manifest_hashes_match_and_collection_does_not_change_existing_records(self):
        before = self.store.database.read_bytes()
        archive = files(diagnostics.build_support_bundle(self.data, "1", now_ms=NOW))
        self.assertEqual(self.store.database.read_bytes(), before)
        manifest = json.loads(archive["manifest.json"])
        self.assertEqual(set(manifest["files"]), set(archive) - {"manifest.json"})
        for name, info in manifest["files"].items():
            self.assertEqual(info["sha256"], diagnostics.hashlib.sha256(archive[name]).hexdigest())
        self.assertEqual(manifest["selection"], "1")
        self.assertFalse(manifest["privacy"]["raw_payloads"])

    def test_malformed_or_oversized_log_input_is_flagged_and_never_copied_raw(self):
        logger = diagnostics.sink(self.data)
        logger.directory.mkdir()
        logger.paths()[0].write_bytes(b"CANARY_RAW_TEXT\n" + b'x' * 2048)
        with patch.object(diagnostics, "FILE_BYTES", 1024):
            archive = files(diagnostics.build_support_bundle(self.data, "all", now_ms=NOW))
        health = json.loads(archive["health.json"])["diagnostic_logging"]
        self.assertTrue(health["input_limited"])
        self.assertGreater(health["malformed_or_unreadable"], 0)
        self.assertNotIn(b"CANARY_RAW_TEXT", b"".join(archive.values()))

    def test_standalone_export_works_without_site_packages_and_refuses_overwrite(self):
        target = self.data / "support.zip"
        args = [sys.executable, "-I", "-S", "-B", str(Path(diagnostics.__file__)),
                "--data-dir", str(self.data), "--days", "all", "--output", str(target)]
        first = subprocess.run(args, capture_output=True, timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        original = target.read_bytes()
        self.assertIn("manifest.json", files(original))
        second = subprocess.run(args, capture_output=True, timeout=20)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(target.read_bytes(), original)

    def test_server_logging_keeps_exception_type_and_discards_formatted_messages(self):
        try:
            raise ValueError("CANARY_EXCEPTION_MESSAGE")
        except ValueError:
            record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1,
                                       "Request CANARY_PATH password=%s", ("CANARY_LOG_ARGUMENT",), sys.exc_info())
            diagnostics.SafeServerErrorHandler(self.data).emit(record)
        result = files(diagnostics.build_support_bundle(self.data, "all"))
        self.assertNotIn(b"CANARY", b"".join(result.values()))
        self.assertIn(b"ValueError", result["events.jsonl"])

    def test_protocol_failures_keep_codes_without_server_messages_or_credentials(self):
        from platelog.alarms import pulse_axis
        from platelog.email_alerts import DEFAULT_SMTP, test_smtp_connection
        config = {**DEFAULT_SMTP, "host": "CANARY_MAIL_HOST", "security": "tls"}
        with patch("platelog.email_alerts.smtplib.SMTP_SSL", side_effect=smtplib.SMTPAuthenticationError(535, b"CANARY_SERVER_REPLY")):
            self.assertEqual(test_smtp_connection(config, data_dir=self.data)[0], "failed")
        connection = MagicMock()
        connection.connect.side_effect = ConnectionRefusedError(10061, "CANARY_DEVICE_ADDRESS")
        with patch.dict("os.environ", {"PLATELOG_AXIS_RELAY_PASSWORD_TEST": "CANARY_DEVICE_PASSWORD"}), \
                patch("platelog.alarms.http.client.HTTPSConnection", return_value=connection):
            result = pulse_axis("CANARY_AXIS_HOST", 1, 1, "CANARY_AXIS_USER", "PLATELOG_AXIS_RELAY_PASSWORD_TEST",
                                data_dir=self.data, camera="CANARY_CAMERA_LOCATION")
            self.assertEqual(result[0], "failed")
        output = files(diagnostics.build_support_bundle(self.data, "1"))
        self.assertNotIn(b"CANARY", b"".join(output.values()))
        rows = [json.loads(line) for line in output["events.jsonl"].splitlines()]
        self.assertEqual(rows[0]["errors"][0]["smtp_code"], 535)
        self.assertEqual(rows[1]["errors"][0]["errno"], 10061)
        self.assertTrue(rows[1]["errors"][0]["frames"])


class AdminDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.store = Store(self.data)
        self.password = "Synthetic Admin Password 2026!"
        set_admin_password(self.store, self.password)
        self.store.set_register_access(True, password_verifier("Synthetic Staff Password 2026!"))
        self.app = make_preview(self.data, "127.0.0.1", persistent=True, allow_http_admin=True)

    def request(self, method, path, fields=None, *, cookie="", origin="http://127.0.0.1"):
        async def call():
            target = urlsplit(path)
            body = urlencode(fields).encode() if fields is not None else b""
            headers = [(b"host", b"127.0.0.1"), (b"origin", origin.encode()), (b"cookie", cookie.encode()),
                       (b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", str(len(body)).encode())]
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
                     "method": method, "scheme": "http", "path": target.path, "raw_path": target.path.encode(),
                     "query_string": target.query.encode(), "headers": headers, "root_path": "",
                     "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 80)}
            messages, sent = [], False
            finished = asyncio.Event()
            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await finished.wait()
                return {"type": "http.disconnect"}
            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and not message.get("more_body"):
                    finished.set()
            await asyncio.wait_for(self.app(scope, receive, send), 10)
            first = next(message for message in messages if message["type"] == "http.response.start")
            return first["status"], first["headers"], b"".join(message.get("body", b"") for message in messages)
        return asyncio.run(call())

    def sign_in(self, path, password):
        code, headers, _ = self.request("POST", path, {"password": password})
        self.assertEqual(code, 303)
        values = {}
        for name, value in headers:
            if name == b"set-cookie":
                jar = SimpleCookie(value.decode())
                values.update({key: item.value for key, item in jar.items() if item["max-age"] != "0"})
        return "; ".join(f"{key}={value}" for key, value in values.items())

    def test_download_requires_administrator_origin_and_csrf(self):
        path = "/admin/diagnostics/download"
        self.assertEqual(self.request("POST", path, {"days": "1"})[0], 403)
        staff = self.sign_in("/login", "Synthetic Staff Password 2026!")
        self.assertEqual(self.request("POST", path, {"days": "1"}, cookie=staff)[0], 403)
        admin = self.sign_in("/admin/login", self.password)
        _, _, page = self.request("GET", "/admin?section=diagnostics", cookie=admin)
        csrf = diagnostics.re.search(rb'name="csrf" value="([^"]+)"', page)[1].decode()
        self.assertEqual(self.request("POST", path, {"days": "1", "csrf": "bad"}, cookie=admin)[0], 403)
        self.assertEqual(self.request("POST", path, {"days": "1", "csrf": csrf}, cookie=admin, origin="https://wrong.example")[0], 403)
        self.assertEqual(self.request("POST", path, {"days": "999", "csrf": csrf}, cookie=admin)[0], 400)
        with diagnostics.DOWNLOAD_LOCK:
            self.assertEqual(self.request("POST", path, {"days": "1", "csrf": csrf}, cookie=admin)[0], 429)
        code, headers, body = self.request("POST", path, {"days": "all", "csrf": csrf}, cookie=admin)
        self.assertEqual(code, 200)
        self.assertIn(b"application/zip", dict(headers)[b"content-type"])
        self.assertEqual(dict(headers)[b"cache-control"], b"no-store")
        self.assertEqual(json.loads(files(body)["manifest.json"])["selection"], "all")


if __name__ == "__main__":
    unittest.main()
