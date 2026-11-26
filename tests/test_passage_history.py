import base64
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from platelog.axis import parse_event
from platelog.simulator import demo_jpeg, payload
from platelog.store import Store

NOW = 1_800_000_000_000
DAY = 86_400_000


class PassageHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.store = Store(self.data)
        self.sequence = 0

    def register(self, *plates):
        known = {plate.lower(): plate for plate in plates}
        preview = self.store.registration_import(known, True)
        return self.store.registration_import(known, True, preview["revision"])

    def event(self, plate="L19MXB", *, at=NOW, track=None, camera="entry", state="new", photo=False):
        self.sequence += 1
        body = payload(at, self.sequence, plate, state)
        body["carID"] = track or str(self.sequence)
        if photo:
            body["ImageArray"] = [{"BinaryImage": base64.b64encode(demo_jpeg(plate)).decode()}]
        event = parse_event("application/json", json.dumps(body).encode())
        with patch("platelog.store.time.time_ns", return_value=at * 1_000_000):
            saved = self.store.save(camera, event)
        return saved, event

    def rows(self, **filters):
        rows, _, _ = self.store.passage_page(filters.pop("plate", ""), filters.pop("camera", ""), 1, 50, **filters)
        return {row["id"]: row for row in rows}

    def test_first_unregistered_pass_has_no_previous_visits_and_replays_do_not_add_any(self):
        self.register("OTHER01")
        first, event = self.event(track="track")
        self.assertTrue(self.store.save("entry", event)["duplicate"])
        update, _ = self.event(at=NOW + 500, track="track", state="update")
        self.assertEqual(first["passage_id"], update["passage_id"])
        row = self.rows()[first["passage_id"]]
        self.assertEqual((row["unregistered_passes"], row["history_unknown"], row["packets"]), (0, 0, 2))
        self.assertFalse(row["registered_at_read"])
        self.assertEqual(row["history_days"], 30)

    def test_next_days_registered_pass_keeps_two_previous_unregistered_passes(self):
        self.register("OTHER01")
        first, _ = self.event(at=NOW - 2 * DAY)
        second, _ = self.event(at=NOW - DAY)
        self.register("OTHER01", "L19MXB")
        today, _ = self.event()
        rows = self.rows()
        self.assertEqual([rows[item["passage_id"]]["unregistered_passes"] for item in (first, second, today)], [0, 1, 2])
        self.assertTrue(all(row["registered"] for row in rows.values()))
        self.assertFalse(rows[first["passage_id"]]["registered_at_read"])
        self.assertTrue(rows[today["passage_id"]]["registered_at_read"])
        self.assertEqual(Store(self.data).passage_page("L19MXB", "", 1, 50)[0][0]["unregistered_passes"], 2)

    def test_scheduled_csv_replacement_preserves_history(self):
        self.register("OTHER01")
        first, _ = self.event(at=NOW - DAY)
        self.store.save_registered_refresh_schedule({"enabled": True, "source": "C:\\Imports\\vehicles.csv", "run_time": "01:15"},
                                                    "2027-01-15T00:00:00+00:00")
        revision = json.loads(self.store.setting("registered_refresh_schedule"))["revision"]
        self.store.finish_registered_refresh(revision, "2027-01-15", "2027-01-15T01:15:00+00:00", {"l19mxb": "L19MXB"})
        today, _ = self.event()
        rows = self.rows()
        self.assertEqual(rows[today["passage_id"]]["unregistered_passes"], 1)
        self.assertFalse(rows[first["passage_id"]]["registered_at_read"])
        self.assertTrue(rows[today["passage_id"]]["registered_at_read"])

    def test_same_plate_update_after_csv_import_does_not_reclassify_the_passage(self):
        self.register("OTHER01")
        first, _ = self.event(track="track")
        self.register("L19MXB")
        update, _ = self.event(at=NOW + 1000, track="track", state="update")
        self.assertEqual(first["passage_id"], update["passage_id"])
        row = self.rows()[first["passage_id"]]
        self.assertTrue(row["registered"])
        self.assertFalse(row["registered_at_read"])
        self.assertEqual(row["unregistered_passes"], 0)

    def test_ocr_correction_uses_original_membership_even_when_the_list_changed(self):
        self.register("L19MXB")
        first, _ = self.event("MISREAD", track="track")
        self.register("OTHER01")
        correction, _ = self.event("L19 MXB", at=NOW + 500, track="track", state="update")
        self.assertEqual(first["passage_id"], correction["passage_id"])
        row = self.rows()[first["passage_id"]]
        self.assertFalse(row["registered"])
        self.assertTrue(row["registered_at_read"])
        self.assertEqual(row["unregistered_passes"], 0)
        later, _ = self.event(at=NOW + 5000)
        self.assertEqual(self.rows()[later["passage_id"]]["unregistered_passes"], 0)

    def test_correction_to_newly_added_plate_keeps_its_original_unregistered_status(self):
        self.register("OTHER01")
        first, _ = self.event("MISREAD", track="track")
        self.register("L19MXB")
        self.event(at=NOW + 500, track="track", state="update")
        row = self.rows()[first["passage_id"]]
        self.assertEqual((row["registered"], row["registered_at_read"], row["unregistered_passes"]), (True, False, 0))
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM passages WHERE history_plate_key='misread'").fetchone()[0], 0)

    def test_delayed_packet_cannot_change_the_effective_plate_or_frozen_status(self):
        self.register("L19MXB")
        first, _ = self.event(track="track")
        self.event(at=NOW + 1000, track="track", state="update")
        self.register("OTHER01")
        self.event("MISREAD", at=NOW + 500, track="track", state="update")
        row = self.rows()[first["passage_id"]]
        self.assertEqual((row["plate"], row["registered_at_read"], row["unregistered_passes"], row["packets"]), ("L19MXB", True, 0, 3))

    def test_window_boundaries_and_filters_use_site_wide_history_up_to_each_row(self):
        self.register("OTHER01")
        oldest, _ = self.event(at=NOW - 31 * DAY)
        self.event(at=NOW - 30 * DAY)
        self.event(at=NOW - 29 * DAY, camera="exit")
        latest, _ = self.event()
        row = self.rows(plate="L19", camera="entry", start_ms=NOW - DAY)[latest["passage_id"]]
        self.assertEqual(row["unregistered_passes"], 2)
        self.store.set_unregistered_history_days(90)
        self.assertEqual(self.rows()[latest["passage_id"]]["unregistered_passes"], 3)
        self.store.set_unregistered_history_days(7)
        rows = self.rows()
        self.assertEqual(rows[latest["passage_id"]]["unregistered_passes"], 0)
        self.assertEqual(rows[oldest["passage_id"]]["unregistered_passes"], 0)
        for days in (0, 3651, -1, True, "30"):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.store.set_unregistered_history_days(days)

    def test_no_registered_list_records_unknown_instead_of_inventing_unregistered_history(self):
        first, _ = self.event(track="track")
        self.register("L19MXB")
        self.event(at=NOW + 500, track="track", state="update")
        today, _ = self.event(at=NOW + 5000)
        rows = self.rows()
        self.assertIsNone(rows[first["passage_id"]]["registered_at_read"])
        self.assertEqual((rows[today["passage_id"]]["unregistered_passes"], rows[today["passage_id"]]["history_unknown"]), (0, 1))

    def test_expired_photos_do_not_remove_passage_history_and_purge_uses_retained_records(self):
        self.register("OTHER01")
        old, _ = self.event(at=NOW - 100 * DAY, photo=True)
        latest, _ = self.event()
        self.store.set_unregistered_history_days(120)
        self.assertEqual(self.rows()[latest["passage_id"]]["unregistered_passes"], 1)
        passages, photos = self.store.apply_retention(NOW)
        self.assertEqual((passages, photos), (0, 1))
        rows = self.rows()
        self.assertEqual(rows[latest["passage_id"]]["unregistered_passes"], 1)
        self.assertTrue(rows[old["passage_id"]]["photo_expired"])
        self.store.set_setting("retention", json.dumps({"enabled": True, "passage_days": 90, "photo_days": 30}), "Synthetic limits")
        self.store.apply_retention(NOW)
        self.assertEqual(self.rows()[latest["passage_id"]]["unregistered_passes"], 0)

    def test_membership_intervals_needed_for_retained_corrections_survive_cleanup(self):
        self.register("L19MXB")
        first, _ = self.event("MISREAD", track="track")
        self.register("OTHER01")
        self.store.apply_retention(NOW + 1000)
        self.event("L19MXB", at=NOW + 1500, track="track", state="update")
        self.assertTrue(self.rows()[first["passage_id"]]["registered_at_read"])

    def test_legacy_migration_is_concurrent_safe_and_does_not_guess_statuses(self):
        self.register("L19MXB")
        saved, _ = self.event()
        with closing(self.store.connect()) as db, db:
            db.execute("DROP INDEX passage_registration_history")
            for column in ("history_plate_key", "registered_at_read", "registration_generation"):
                db.execute(f"ALTER TABLE passages DROP COLUMN {column}")
            db.execute("DROP TABLE registration_membership")
            db.execute("DELETE FROM settings WHERE name='registration_generation'")
        with ThreadPoolExecutor(max_workers=2) as pool:
            stores = list(pool.map(lambda _: Store(self.data), range(2)))
        row = stores[0].passage_page("L19MXB", "", 1, 50)[0][0]
        self.assertEqual(row["id"], saved["passage_id"])
        self.assertTrue(row["registered"])
        self.assertIsNone(row["registered_at_read"])
        self.assertEqual((row["unregistered_passes"], row["history_unknown"]), (0, 0))

    def test_registered_visits_never_increment_the_unregistered_history(self):
        self.register("OTHER01")
        self.event(at=NOW - 5 * DAY)
        self.register("L19MXB")
        self.event(at=NOW - 4 * DAY)
        self.event(at=NOW - 3 * DAY)
        self.register("OTHER01")
        latest, _ = self.event()
        row = self.rows()[latest["passage_id"]]
        self.assertFalse(row["registered"])
        self.assertEqual(row["unregistered_passes"], 1)
