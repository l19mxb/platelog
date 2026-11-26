import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from platelog.axis import parse_event
from platelog.simulator import payload
from platelog.store import Store


NOW = 1_800_000_000_000


class AudibleTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.data = Path(scratch.name)
        self.store = Store(self.data)
        self.store.save_camera("entry", "none", "", "")
        self.store.save_camera("exit", "none", "", "")

    def configure(self, registered=True, unregistered=True):
        config = {"registered": registered, "unregistered": unregistered}
        self.store.set_setting("audible_alerts", json.dumps(config), "Synthetic sound configuration")

    def register(self):
        plates = {"l19mxb": "L19MXB"}
        review = self.store.registration_import(plates, False)
        self.store.registration_import(plates, False, review["revision"])

    def event(self, plate, counter, *, camera="entry", at=NOW, capture=None, track=None, state="new"):
        body = payload(at if capture is None else capture, counter, plate, state)
        body["carID"] = str(counter) if track is None else track
        event = parse_event("application/json", json.dumps(body).encode())
        with patch("platelog.store.time.time_ns", return_value=at * 1_000_000):
            return self.store.save(camera, event)

    def events(self, since=NOW - 1, camera="", at=NOW + 2500):
        return self.store.audible_events(since, camera, now_ms=at)["events"]

    def test_defaults_and_saved_settings_survive_restart(self):
        self.assertEqual(self.store.audible_settings(), {"registered": False, "unregistered": False})
        self.register()
        self.event("L19MXB", 1)
        self.assertEqual(self.events(), [])
        self.configure(registered=False)
        reopened = Store(self.data)
        self.assertEqual(reopened.audible_settings(), {"registered": False, "unregistered": True})
        self.assertEqual(reopened.alarm_overview(), ([], []))

    def test_fresh_reads_and_selected_camera_use_current_plate_status(self):
        self.register()
        self.configure()
        known = self.event("L19-MXB", 1)
        unknown = self.event("SIM-01", 2, camera="exit")
        kinds = {row["id"]: row["kind"] for row in self.events()}
        self.assertEqual(kinds, {known["passage_id"]: "registered", unknown["passage_id"]: "unregistered"})
        self.assertEqual([row["id"] for row in self.events(camera="exit")], [unknown["passage_id"]])
        self.configure(registered=False)
        self.assertEqual([row["id"] for row in self.events()], [unknown["passage_id"]])

    def test_starting_sound_and_replay_do_not_create_extra_events(self):
        self.register()
        self.configure()
        first = self.event("SIM-01", 1)
        self.assertEqual(self.events(since=None), [])
        self.assertEqual(self.events(since=NOW), [])
        self.assertTrue(self.event("SIM-01", 1, at=NOW + 1000, capture=NOW)["duplicate"])
        self.assertEqual(self.events(), [{"id": first["passage_id"], "received_ms": NOW, "kind": "unregistered"}])
        self.assertEqual(self.events(since=NOW + 1), [])

    def test_plate_correction_waits_for_settlement(self):
        self.register()
        self.configure()
        first = self.event("MISREAD", 1, track="same-track")
        corrected = self.event("L19MXB", 2, at=NOW + 800, track="same-track", state="update")
        self.assertEqual(first["passage_id"], corrected["passage_id"])
        self.assertEqual(self.events(at=NOW + 2799), [])
        self.assertEqual(self.events(at=NOW + 2800), [
            {"id": first["passage_id"], "received_ms": NOW, "kind": "registered"}
        ])

    def test_empty_list_stale_future_and_ambiguous_reads_stay_quiet(self):
        self.configure()
        self.event("SIM-01", 1)
        self.assertEqual(self.events(), [])
        self.register()
        stale = self.event("SIM-OLD", 2, capture=NOW - 120_000)
        future = self.event("SIM-FUTURE", 3, capture=NOW + 10_000)
        ambiguous = self.event("SIM-REVIEW", 4)
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE passages SET review=1 WHERE id=?", (ambiguous["passage_id"],))
        ids = {row["id"] for row in self.events()}
        self.assertTrue(ids.isdisjoint({stale["passage_id"], future["passage_id"], ambiguous["passage_id"]}))
