import json
import ssl
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from platelog.alarms import SETTLE_MS, parse_rule, pulse_axis, run_due_alarms
from platelog.axis import parse_event
from platelog.simulator import payload
from platelog.store import Store

NOW = 1_800_000_000_000
SECRET = "PLATELOG_AXIS_RELAY_PASSWORD_TEST"
COMMAND = "https://a9210.example.internal/axis-cgi/virtualinput/activate.cgi?schemaversion=1&port=2&duration=1"
SETTINGS = {"mode": "log", "trigger": "unregistered", "command": COMMAND,
            "username": "qa-operator", "secret_name": SECRET}


class AlarmTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.store = Store(self.data)
        self.store.save_camera("entry", "none", "", "")
        self.store.save_camera("exit", "none", "", "")

    def register_known_plate(self):
        known = {"l19mxb": "L19MXB"}
        preview = self.store.registration_import(known, False)
        self.store.registration_import(known, False, preview["revision"])

    def set_mode(self, mode, trigger="unregistered"):
        with patch.dict("os.environ", {SECRET: "disposable operator secret"}):
            self.store.save_alarm_rule("entry", parse_rule({**SETTINGS, "mode": mode, "trigger": trigger}))

    def event(self, time_ms, sequence, plate, track, *, state="new", camera="entry", captured_ms=None):
        body = payload(time_ms if captured_ms is None else captured_ms, sequence, plate, state)
        body["carID"] = track
        event = parse_event("application/json", json.dumps(body).encode())
        with patch("platelog.store.time.time_ns", return_value=time_ms * 1_000_000):
            return self.store.save(camera, event)

    def decisions(self):
        return {row["passage_id"]: (row["result"], row["plate"]) for row in self.store.alarm_overview()[1]}

    def test_log_only_settles_corrected_plates_and_deduplicates(self):
        self.register_known_plate()
        self.set_mode("log")
        unknown = self.event(NOW, 1, "SIM-01", "t1")
        self.assertTrue(self.event(NOW, 1, "SIM-01", "t1")["duplicate"])
        corrected = self.event(NOW, 2, "MISREAD", "t2")
        update = self.event(NOW + 800, 3, "L19-MXB", "t2", state="update")
        self.assertEqual(update["passage_id"], corrected["passage_id"])
        self.event(NOW, 4, "SIM-02", "t3", camera="exit")
        with patch("platelog.alarms.pulse_axis") as pulse:
            run_due_alarms(self.store, NOW + SETTLE_MS - 1)
            self.assertEqual(self.decisions(), {})
            run_due_alarms(self.store, NOW + SETTLE_MS)
            self.assertEqual(self.decisions(), {unknown["passage_id"]: ("logged", "SIM-01")})
            run_due_alarms(self.store, NOW + SETTLE_MS + 800)
            pulse.assert_not_called()
        self.assertEqual(self.decisions()[corrected["passage_id"]], ("registered", "L19-MXB"))
        self.assertEqual(len(self.decisions()), 2)

    def test_live_queue_claims_once_and_preserves_an_unconfirmed_result(self):
        self.register_known_plate()
        self.set_mode("live")
        event = self.event(NOW, 1, "SIM-01", "t1")
        reopened = Store(self.data)
        with patch("platelog.alarms.pulse_axis", return_value=("unconfirmed", "Axis did not reply.")) as pulse:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda store: run_due_alarms(store, NOW + SETTLE_MS), (self.store, reopened)))
            run_due_alarms(reopened, NOW + SETTLE_MS + 9_000)
            pulse.assert_called_once_with("a9210.example.internal", 2, 1, "qa-operator", SECRET,
                                          data_dir=self.store.database.parent, camera="entry")
        self.assertEqual(self.decisions()[event["passage_id"]], ("unconfirmed", "SIM-01"))

    def test_rule_edits_empty_list_and_stale_captures_hold_pulses(self):
        self.set_mode("live")
        empty = self.event(NOW, 1, "SIM-01", "t1")
        with patch("platelog.alarms.pulse_axis") as pulse:
            run_due_alarms(self.store, NOW + SETTLE_MS)
            self.register_known_plate()
            stale = self.event(NOW, 2, "SIM-02", "t2", captured_ms=NOW - 120_000)
            run_due_alarms(self.store, NOW + SETTLE_MS)
            pending = self.event(NOW, 3, "SIM-03", "t3")
            self.set_mode("off")
            run_due_alarms(self.store, NOW + SETTLE_MS)
            pulse.assert_not_called()
        self.assertEqual({key: "held" for key in (empty["passage_id"], stale["passage_id"], pending["passage_id"])},
                         {key: value[0] for key, value in self.decisions().items()})

    def test_live_requires_a_rule_username_and_service_scoped_password(self):
        live = parse_rule({**SETTINGS, "mode": "live"})
        with patch.dict("os.environ", {SECRET: ""}), self.assertRaisesRegex(ValueError, "Live mode needs"):
            self.store.save_alarm_rule("entry", live)
        no_user = parse_rule({**SETTINGS, "mode": "live", "username": ""})
        with patch.dict("os.environ", {SECRET: "disposable operator secret"}), self.assertRaisesRegex(ValueError, "Live mode needs"):
            self.store.save_alarm_rule("entry", no_user)
        with self.assertRaisesRegex(ValueError, "environment variable"):
            parse_rule({**SETTINGS, "secret_name": "PATH"})

    def test_rules_use_their_own_destinations_and_credentials(self):
        self.register_known_plate()
        other_secret = SECRET + "_2"
        other = {**SETTINGS, "mode": "live", "trigger": "registered", "username": "camera-operator",
                 "secret_name": other_secret, "command": COMMAND.replace("a9210.", "camera.")}
        with patch.dict("os.environ", {SECRET: "entry secret", other_secret: "exit secret"}):
            self.store.save_alarm_rule("entry", parse_rule({**SETTINGS, "mode": "live"}))
            self.store.save_alarm_rule("exit", parse_rule(other))
        self.event(NOW, 1, "SIM-01", "t1")
        self.event(NOW, 2, "L19MXB", "t2", camera="exit")
        with patch("platelog.alarms.pulse_axis", return_value=("accepted", "Axis accepted.")) as pulse:
            run_due_alarms(self.store, NOW + SETTLE_MS)
        self.assertCountEqual(pulse.call_args_list, [
            call("a9210.example.internal", 2, 1, "qa-operator", SECRET, data_dir=self.data, camera="entry"),
            call("camera.example.internal", 2, 1, "camera-operator", other_secret, data_dir=self.data, camera="exit"),
        ])

    def test_pasted_command_must_be_a_timed_https_virtual_input_url(self):
        for command in (COMMAND.replace("https://", "http://"), COMMAND.replace("activate.cgi", "deactivate.cgi"),
                        COMMAND.replace("://", "://operator:secret@"), COMMAND.replace("&duration=1", ""),
                        COMMAND.replace("duration=1", "duration=0"), COMMAND + "&port=3"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                parse_rule({**SETTINGS, "command": command})

    def test_registered_trigger_matches_registered_plates(self):
        self.register_known_plate()
        self.set_mode("log", "registered")
        known = self.event(NOW, 1, "L19-MXB", "t1")
        unknown = self.event(NOW, 2, "SIM-01", "t2")
        with patch("platelog.alarms.pulse_axis") as pulse:
            run_due_alarms(self.store, NOW + SETTLE_MS)
            pulse.assert_not_called()
        self.assertEqual(self.decisions(), {known["passage_id"]: ("logged", "L19-MXB"),
                                            unknown["passage_id"]: ("unregistered", "SIM-01")})

    def test_vapix_request_and_200_error_response(self):
        with patch.dict("os.environ", {SECRET: "disposable operator secret"}), \
                patch("platelog.alarms.http.client.HTTPSConnection") as connection:
            client = connection.return_value
            result = MagicMock()
            client.getresponse.return_value.__enter__.return_value = result
            result.status = 200
            prefix = b'<VirtualInputResponse xmlns="http://www.axis.com/vapix/http_cgi/virtualinput1">'
            suffix = b'</VirtualInputResponse>'
            result.read.return_value = prefix + (
                b'<Success><ActivateSuccess><StateChanged>true</StateChanged></ActivateSuccess></Success>'
            ) + suffix
            self.assertEqual(pulse_axis("a9210.example.internal", 2, 1, "qa-operator", SECRET)[0], "accepted")
            self.assertEqual(connection.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(client.request.call_args.args,
                             ("GET", "/axis-cgi/virtualinput/activate.cgi?schemaversion=1&port=2&duration=1"))
            self.assertTrue(client.request.call_args.kwargs["headers"]["Authorization"].startswith("Basic "))
            result.read.return_value = prefix + (
                b'<Error><GeneralError><ErrorCode>20</ErrorCode></GeneralError></Error>'
            ) + suffix
            self.assertEqual(pulse_axis("a9210.example.internal", 2, 1, "qa-operator", SECRET)[0], "failed")
            client.getresponse.side_effect = TimeoutError
            self.assertEqual(pulse_axis("a9210.example.internal", 2, 1, "qa-operator", SECRET)[0], "unconfirmed")
