import json
import smtplib
import ssl
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from platelog.alarms import parse_rule, run_due_alarms
from platelog.axis import parse_event
from platelog.email_alerts import (DEFAULT_SMTP, DEFAULT_SUBJECT, mailbox, parse_smtp_settings, recipients,
                                   send_alarm_email, subject_template, test_smtp_connection as check_connection)
from platelog.simulator import payload
from platelog.store import Store

NOW = 1_800_000_000_000
CONFIG = {**DEFAULT_SMTP, "host": "mail.example.internal", "sender": "platelog@example.internal"}
RULE = {"mode": "live", "trigger": "unregistered", "action": "smtp",
        "email_recipients": "gatehouse@example.internal", "email_subject": DEFAULT_SUBJECT}
SECRET = "PLATELOG_SMTP_PASSWORD_TEST"
AXIS_SECRET = "PLATELOG_AXIS_RELAY_PASSWORD_TEST"


class SMTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.store = Store(self.data)
        self.store.save_camera("entry", "none", "", "")
        self.store.save_camera("exit", "none", "", "")
        plates = {"l19mxb": "L19MXB"}
        preview = self.store.registration_import(plates, False)
        self.store.registration_import(plates, False, preview["revision"])

    def event(self, plate="DEMO999", *, at=NOW, track="t1", sequence=1, state="new", camera="entry", capture=None):
        body = payload(at if capture is None else capture, sequence, plate, state)
        body["carID"] = track
        with patch("platelog.store.time.time_ns", return_value=at * 1_000_000):
            return self.store.save(camera, parse_event("application/json", json.dumps(body).encode()))

    def configure(self, **rule):
        self.store.save_smtp_settings(CONFIG)
        with patch.dict("os.environ", {AXIS_SECRET: "synthetic axis password"}):
            self.store.save_alarm_rule("entry", parse_rule({**RULE, **rule}))

    def run_row(self):
        return self.store.alarm_overview()[1][0]

    def test_email_addresses_subjects_and_settings_reject_injection(self):
        self.assertEqual(recipients("gate@example.internal, gate@example.internal"), "gate@example.internal")
        for bad in ("a@example.internal\r\nBcc: stolen@example.internal", "Name <a@example.internal>", "a@example.internal,", "a..b@example.internal"):
            with self.subTest(address=bad), self.assertRaises(ValueError):
                recipients(bad)
        with self.assertRaises(ValueError):
            recipients(",".join(f"test{i}@example.internal" for i in range(11)))
        for bad in ("Alarm\nBcc: x", "{plate.__class__}", "{unknown}", "{plate", "x" * 201):
            with self.subTest(subject=bad), self.assertRaises(ValueError):
                subject_template(bad)
        self.assertEqual(subject_template(""), DEFAULT_SUBJECT)
        for override in ({"host": "https://mail.example.internal"}, {"port": "0"}, {"port": "65536"},
                         {"security": "none", "username": "a", "secret_name": SECRET},
                         {"username": "a"}, {"username": "a", "secret_name": "plaintext-password"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                parse_smtp_settings({**CONFIG, **override})
        self.assertEqual(parse_smtp_settings({**CONFIG, "security": "none", "port": "25"})["port"], 25)

    def test_starttls_auth_and_message_use_verified_tls(self):
        config = {**CONFIG, "username": "smtp-user", "secret_name": SECRET}
        run = {"plate": "L19MXB", "camera": "entry", "plate_status": "registered", "passage_id": "a" * 32,
               "email_recipients": RULE["email_recipients"], "email_subject": DEFAULT_SUBJECT,
               "capture_ms": NOW, "received_ms": NOW + 1234}
        with patch("platelog.email_alerts.smtplib.SMTP") as smtp, patch.dict("os.environ", {SECRET: "disposable mail password"}):
            client = smtp.return_value
            client.send_message.return_value = {}
            self.assertEqual(send_alarm_email(config, run, "Australia/Sydney")[0], "accepted")
            client.login.assert_called_once_with("smtp-user", "disposable mail password")
            self.assertEqual(client.starttls.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(client.starttls.call_args.kwargs["context"].check_hostname)
            message = client.send_message.call_args.args[0]
            self.assertEqual(str(message["Subject"]), "[PlateLog] Registered: L19MXB")
            self.assertIn("Time zone: Australia/Sydney", message.get_content())
            self.assertIn("+11:00", message.get_content())
            self.assertFalse(message.is_multipart())
            client.close.assert_called_once()
        with patch("platelog.email_alerts.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.send_message.return_value = {}
            self.assertEqual(send_alarm_email({**CONFIG, "security": "tls", "port": 465}, run, "UTC")[0], "accepted")
            self.assertEqual(smtp.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
            smtp.return_value.starttls.assert_not_called()

    def test_connection_test_sends_no_mail_and_closes_on_failures(self):
        with patch("platelog.email_alerts.smtplib.SMTP") as smtp:
            client = smtp.return_value
            client.noop.return_value = (250, b"OK")
            self.assertEqual(check_connection(CONFIG)[0], "accepted")
            client.send_message.assert_not_called()
            client.close.assert_called_once()
        for exception in (ssl.SSLCertVerificationError(), smtplib.SMTPAuthenticationError(535, b"secret detail"),
                          smtplib.SMTPNotSupportedError("secret detail"), TimeoutError()):
            with self.subTest(error=type(exception).__name__), patch("platelog.email_alerts.smtplib.SMTP", side_effect=exception):
                result, detail = check_connection(CONFIG)
                self.assertEqual(result, "failed")
                self.assertNotIn("secret detail", detail)

    def test_email_only_uses_latest_corrected_plate_and_dispatches_once(self):
        self.configure(trigger="registered")
        event = self.event("MISREAD")
        self.event("L19MXB", at=NOW + 800, sequence=2, state="update")
        reopened = Store(self.data)
        with patch("platelog.alarms.send_alarm_email", return_value=("accepted", "SMTP accepted.")) as email, \
                patch("platelog.alarms.pulse_axis") as axis:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda store: run_due_alarms(store, NOW + 2800), (self.store, reopened)))
            run_due_alarms(reopened, NOW + 9000)
            email.assert_called_once()
            axis.assert_not_called()
            self.assertEqual(email.call_args.args[1]["plate"], "L19MXB")
            self.assertEqual(email.call_args.args[1]["capture_ms"], NOW + 800)
        self.assertEqual(self.run_row()["passage_id"], event["passage_id"])
        self.assertEqual(json.loads(self.run_row()["deliveries"])["smtp"]["result"], "accepted")

    def test_both_actions_record_independent_results_without_retry(self):
        self.configure(action="both", command="https://axis.example.internal/axis-cgi/virtualinput/activate.cgi?schemaversion=1&port=1&duration=1",
                       username="axis-operator", secret_name=AXIS_SECRET)
        first = self.event()
        self.assertTrue(self.event()["duplicate"])
        with patch("platelog.alarms.pulse_axis", return_value=("unconfirmed", "Axis timeout.")) as axis, \
                patch("platelog.alarms.send_alarm_email", return_value=("accepted", "SMTP accepted.")) as email:
            run_due_alarms(self.store, NOW + 2000)
            run_due_alarms(Store(self.data), NOW + 5000)
            axis.assert_called_once()
            email.assert_called_once()
        self.assertEqual(self.run_row()["result"], "partial")
        results = json.loads(self.run_row()["deliveries"])
        self.assertEqual(results["vapix"]["result"], "unconfirmed")
        self.assertEqual(results["smtp"]["result"], "accepted")

    def test_off_log_only_wrong_camera_stale_and_settings_edits_suppress_email(self):
        self.configure(mode="log")
        self.event()
        with patch("platelog.alarms.send_alarm_email") as email:
            run_due_alarms(self.store, NOW + 2000)
            self.assertEqual(self.run_row()["result"], "logged")
            self.configure()
            self.event(track="stale", sequence=2, capture=NOW - 120_000)
            self.event(track="wrong-camera", sequence=3, camera="exit")
            run_due_alarms(self.store, NOW + 2000)
            self.event(track="config-edit", sequence=4)
            self.store.save_smtp_settings({**CONFIG, "port": 2525})
            run_due_alarms(self.store, NOW + 2000)
            self.event(track="off", sequence=5)
            self.configure(mode="off")
            run_due_alarms(self.store, NOW + 2000)
            email.assert_not_called()

    def test_live_email_requires_config_and_credentials_without_saving_secrets(self):
        with self.assertRaisesRegex(ValueError, "SMTP settings"):
            self.store.save_alarm_rule("entry", parse_rule(RULE))
        self.store.save_smtp_settings({**CONFIG, "username": "smtp-user", "secret_name": SECRET})
        with patch.dict("os.environ", {SECRET: ""}), self.assertRaisesRegex(ValueError, "SMTP settings"):
            self.store.save_alarm_rule("entry", parse_rule(RULE))
        with patch.dict("os.environ", {SECRET: "never-store-this-value"}):
            self.store.save_alarm_rule("entry", parse_rule(RULE))
        self.assertNotIn("never-store-this-value", self.store.setting("smtp_settings"))

    def test_partial_recipient_refusal_and_timeouts_are_not_success(self):
        run = {"plate": "DEMO999", "camera": "entry", "plate_status": "unregistered", "passage_id": "b" * 32,
               "email_recipients": "a@example.internal, b@example.internal", "email_subject": DEFAULT_SUBJECT,
               "capture_ms": NOW, "received_ms": NOW}
        with patch("platelog.email_alerts.smtplib.SMTP") as smtp:
            smtp.return_value.send_message.return_value = {"b@example.internal": (550, b"rejected")}
            self.assertEqual(send_alarm_email(CONFIG, run, "UTC")[0], "partial")
            smtp.return_value.send_message.side_effect = TimeoutError()
            self.assertEqual(send_alarm_email(CONFIG, run, "UTC")[0], "unconfirmed")
            smtp.return_value.send_message.side_effect = smtplib.SMTPRecipientsRefused({"a@example.internal": (550, b"rejected")})
            self.assertEqual(send_alarm_email(CONFIG, run, "UTC")[0], "failed")

    def test_legacy_rules_migrate_as_vapix_with_the_same_revision(self):
        original = parse_rule({"mode": "log", "trigger": "unregistered", "command": ""})
        self.store.save_alarm_rule("entry", original)
        revision = self.store.alarm_overview()[0][0]["revision"]
        with closing(self.store.connect()) as db, db:
            for name in ("action", "email_recipients", "email_subject"):
                db.execute(f"ALTER TABLE alarm_rules DROP COLUMN {name}")
            db.execute("ALTER TABLE alarm_runs DROP COLUMN deliveries")
        reopened = Store(self.data)
        row = reopened.alarm_overview()[0][0]
        self.assertEqual((row["action"], row["email_recipients"], row["revision"]), ("vapix", "", revision))
        self.assertEqual(Store(self.data).alarm_overview()[0][0]["revision"], revision)
