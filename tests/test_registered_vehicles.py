import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from platelog.axis import parse_event
from platelog.register_stats import DAY_MS, HOUR_MS, render_stats
from platelog.registered_refresh import refresh_due_registrations, refresh_schedule, refresh_status, save_refresh_schedule
from platelog.simulator import payload
from platelog.store import RegistrationChanged, Store
from platelog.vehicle_import import OFFICE_NS, PACKAGE_NS, SHEET_NS, parse_plates


def workbook(plate_cell: str) -> bytes:
    buffer = BytesIO()
    sheet_ns = SHEET_NS[1:-1]
    office_ns = OFFICE_NS[1:-1]
    package_ns = PACKAGE_NS[1:-1]
    with ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{sheet_ns}" xmlns:r="{office_ns}">'
                         '<sheets><sheet name="Plates" sheetId="1" r:id="sheet"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{package_ns}">'
                         f'<Relationship Id="sheet" Type="{office_ns}/worksheet" Target="worksheets/sheet1.xml"/>'
                         f'<Relationship Id="shared" Type="{office_ns}/sharedStrings" Target="/xl/sharedStrings.xml"/>'
                         '</Relationships>')
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{sheet_ns}"><si><t>Plate</t></si><si><t>0012</t></si></sst>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{sheet_ns}"><sheetData>'
                         f'<row r="1"><c r="B1" t="s"><v>0</v></c></row><row r="2">{plate_cell}</row>'
                         '</sheetData></worksheet>')
    return buffer.getvalue()


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))

    def test_replace_is_reviewed_and_atomic(self):
        prior = self.store.registration_import({"abc123": "ABC-123", "def456": "DEF456"}, False)
        self.assertEqual(self.store.registered_count(), 0)
        self.store.registration_import({"abc123": "ABC-123", "def456": "DEF456"}, False, prior["revision"])
        replacement = {"ghi789": "GHI789"}
        draft = self.store.registration_import(replacement, True)
        self.assertEqual((draft["added"], draft["removed"], draft["total"]), (1, 2, 1))
        self.assertEqual(self.store.registered_count(), 2)
        self.store.registration_import({"jkl012": "JKL012"}, False, draft["revision"])
        with self.assertRaises(RegistrationChanged):
            self.store.registration_import(replacement, True, draft["revision"])
        self.assertEqual(self.store.registered_count(), 3)
        current = self.store.registration_import(replacement, True)
        self.store.registration_import(replacement, True, current["revision"])
        self.assertEqual(self.store.registered_count(), 1)
        self.assertEqual(self.store.registration_import(replacement, False)["existing"], 1)

    def test_stats_count_the_latest_plate_per_passage(self):
        end = 1_800_000_000_000

        def save(camera, timestamp, counter, plate, state="new"):
            body = json.dumps(payload(timestamp, counter, plate, state)).encode()
            self.store.save(camera, parse_event("application/json", body))

        before = end - 3 * HOUR_MS
        save("entry", end - DAY_MS - 1, 1, "OLD")
        save("entry", before, 2, "MISREAD")
        save("entry", before + 10, 3, "ABC123", "update")
        save("entry", before + 20 * 60_000, 4, "ABC123")
        save("entry", before + 30 * 60_000, 5, "NEW456")
        save("exit", end - 1000, 6, "NEW456")
        save("exit", end, 7, "FUTURE")
        preview = self.store.registration_import({"abc123": "ABC-123"}, False)
        self.store.registration_import({"abc123": "ABC-123"}, False, preview["revision"])
        rendered = render_stats(self.store, end, "", timezone.utc)
        self.assertIn("data-total>4", rendered)
        self.assertIn("data-unique>2", rendered)
        self.assertIn("data-unregistered>1", rendered)
        self.assertIn("data-busiest>3 ", rendered)
        self.assertIn("data-total>1", render_stats(self.store, end, "exit", timezone.utc))
        self.assertIn("data-busiest>—", render_stats(self.store, end, "missing", timezone.utc))
        rows, _, _ = self.store.passage_page("", "entry", 1, 10, start_ms=end - DAY_MS, end_ms=end)
        self.assertEqual([(row["plate"], row["registered"]) for row in rows],
                         [("NEW456", False), ("ABC123", True), ("ABC123", True)])

    def test_normalized_csv_and_excel_text_plates(self):
        self.assertEqual(parse_plates(b"\xef\xbb\xbfPlate,Note\r\nABC-123,staff\r\nabc123,repeated\r\n0012,\r\n", "csv"),
                         ({"abc123": "ABC-123", "0012": "0012"}, 1))
        self.assertEqual(parse_plates(workbook('<c r="B2" t="s"><v>1</v></c>'), "xlsx"), ({"0012": "0012"}, 0))
        self.assertEqual(parse_plates(workbook('<c r="B2" t="inlineStr"><is><t>0012</t></is></c>'), "xlsx"),
                         ({"0012": "0012"}, 0))
        with self.assertRaisesRegex(ValueError, "Row 2: enter the plate as text"):
            parse_plates(workbook('<c r="B2" t="n"><v>12</v></c>'), "xlsx")
        with self.assertRaisesRegex(ValueError, "Add at least one plate"):
            parse_plates(b"Plate\r\n", "csv")

    def test_daily_refresh_replaces_then_keeps_the_last_good_list(self):
        zone = ZoneInfo("Australia/Sydney")
        configured = datetime(2026, 11, 17, 12, 0, tzinfo=zone)
        overnight = datetime(2026, 11, 18, 1, 15, tzinfo=zone)
        source = self.store.database.with_name("registered.csv")
        self.store.set_setting("display_timezone", zone.key, "Test display time zone")
        original = {"old001": "OLD001"}
        self.store.registration_import(original, False, "")
        source.write_bytes(b"Plate\n0012\nNEW001\n")
        save_refresh_schedule(self.store, True, str(source), "01:15", now=configured)
        refresh_due_registrations(self.store, timezone.utc, configured)
        refresh_due_registrations(self.store, timezone.utc, overnight - timedelta(seconds=1))
        self.assertEqual(refresh_status(self.store), {})
        self.assertEqual(self.store.registered_count(), 1)

        refresh_due_registrations(self.store, timezone.utc, overnight)
        status = refresh_status(self.store)
        last_success = status["last_success"]
        self.assertEqual((last_success["day"], last_success["added"], last_success["removed"], last_success["total"]),
                         ("2026-11-18", 2, 1, 2))
        self.assertEqual(self.store.registration_import(original, False)["existing"], 0)
        source.unlink()
        refresh_due_registrations(self.store, timezone.utc, overnight + timedelta(hours=1))
        self.assertEqual(refresh_status(self.store), status)

        refresh_due_registrations(self.store, timezone.utc, overnight + timedelta(days=1))
        status = refresh_status(self.store)
        self.assertIn("could not find", status["last_attempt"]["error"])
        self.assertEqual(status["last_success"], last_success)
        self.assertEqual(self.store.registered_count(), 2)

        source.write_bytes(b"Plate\n")
        refresh_due_registrations(self.store, timezone.utc, overnight + timedelta(days=2))
        status = refresh_status(self.store)
        self.assertIn("Add at least one plate", status["last_attempt"]["error"])
        self.assertEqual(status["last_success"], last_success)
        self.assertEqual(self.store.registered_count(), 2)

        source.write_bytes(b"Plate\nSHOULDNOTIMPORT\n!?\n")
        refresh_due_registrations(self.store, timezone.utc, overnight + timedelta(days=3))
        status = refresh_status(self.store)
        self.assertIn("Row 3", status["last_attempt"]["error"])
        self.assertEqual(status["last_success"], last_success)
        self.assertEqual(self.store.registration_import({"shouldnotimport": "SHOULDNOTIMPORT"}, False)["existing"], 0)
        self.assertEqual(self.store.registered_count(), 2)

    def test_schedule_edit_during_read_cancels_that_replacement(self):
        source = self.store.database.with_name("registered.csv")
        original = {"old001": "OLD001"}
        self.store.registration_import(original, False, "")
        midnight = datetime(2026, 11, 18, tzinfo=timezone.utc)
        save_refresh_schedule(self.store, True, str(source), "01:15", now=midnight)
        due = midnight + timedelta(hours=1, minutes=15)

        def disable_during_read(path):
            self.assertEqual(path, source)
            save_refresh_schedule(self.store, False, str(source), "01:15", now=due)
            return {"new001": "NEW001"}

        with patch("platelog.registered_refresh.read_plate_source", side_effect=disable_during_read):
            refresh_due_registrations(self.store, timezone.utc, due)
        self.assertFalse(refresh_schedule(self.store)["enabled"])
        self.assertEqual(refresh_status(self.store), {})
        self.assertEqual(self.store.registration_import(original, False)["existing"], 1)
