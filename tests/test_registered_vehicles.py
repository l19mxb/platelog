import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import closing
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from platelog.axis import parse_event
from platelog.import_review import review_content, skipped_csv
from platelog.register_stats import DAY_MS, HOUR_MS, render_stats
from platelog.registered_refresh import refresh_due_registrations, refresh_schedule, refresh_status, save_refresh_schedule
from platelog.simulator import payload
from platelog.store import RegistrationChanged, Store
from platelog.vehicle_import import OFFICE_NS, PACKAGE_NS, SHEET_NS, ImportRejected, PlateImport, parse_plate_import, parse_plates


def workbook(plate_cell: str, *, heading: str = "Plate", extra_headings: str = "") -> bytes:
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
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{sheet_ns}"><si><t>{heading}</t></si><si><t>0012</t></si></sst>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{sheet_ns}"><sheetData>'
                         f'<row r="1"><c r="B1" t="s"><v>0</v></c>{extra_headings}</row><row r="2">{plate_cell}</row>'
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
        self.assertIn("data-unregistered>1", rendered)
        self.assertNotIn("data-unique", rendered)
        self.assertNotIn("data-busiest", rendered)
        self.assertIn('class="stats-line"', rendered)
        self.assertEqual(rendered.count(" · 0 passes</title>"), 92)
        self.assertEqual(rendered.count(" · 1 pass</title>"), 4)
        self.assertIn("data-total>1", render_stats(self.store, end, "exit", timezone.utc))
        empty = render_stats(self.store, end, "missing", timezone.utc)
        self.assertIn("data-total>0", empty)
        self.assertIn("data-unregistered>0", empty)
        self.assertEqual(empty.count(" · 0 passes</title>"), 96)
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

    def test_cardholder_commas_spaces_authorisation_and_shared_vehicles(self):
        source = ("Cardholder Name,PSF #,Vehicle,Cardholder Authorised\r\n"
                  'Private person,1234,"l19 mxb, 00 12, Shared1",Yes\r\n'
                  'Another person,5678,"L19MXB,SHARED1",No\r\n'
                  'Third person,9012,"L19MXB,shared1",yes\r\n'
                  'Fourth person,3456,"NO123,shared1",no\r\n').encode()
        imported = parse_plate_import(source, "csv")
        self.assertEqual(imported.plates, {"l19mxb": "L19MXB", "0012": "0012", "shared1": "SHARED1"})
        self.assertEqual((imported.rows, imported.duplicates, imported.excluded_rows,
                          imported.spaces_removed, imported.shared_plates), (4, 2, 2, 2, 2))
        self.assertEqual(imported.warnings, [])
        self.assertNotIn("Private person", json.dumps(imported.report()))
        self.assertNotIn("1234", json.dumps(imported.report()))

    def test_cardholder_only_commas_split_and_other_punctuation_is_reported(self):
        imported = parse_plate_import(
            b'Vehicle,Cardholder Authorised\n"ABC123 DEF456,L19 MXB,BAD/PLATE,BAD.ONE,,",Yes\n', "csv")
        self.assertEqual(imported.plates, {"abc123def456": "ABC123DEF456", "l19mxb": "L19MXB"})
        self.assertEqual((imported.skipped_entries, imported.spaces_removed), (4, 2))
        self.assertEqual(len(imported.warnings), 4)
        self.assertTrue(all(warning.startswith("Row 2:") for warning in imported.warnings))
        self.assertEqual(parse_plate_import(
            'Vehicle,Cardholder Authorised\n"L19\u00a0MXB,\u00df",Yes\n'.encode(), "csv").plates,
            {"l19mxb": "L19MXB"})

    def test_cardholder_structure_and_authorisation_errors_reject_the_file(self):
        for source, message in (
            (b"Vehicle\nL19MXB\n", "one Vehicle column"),
            (b"Vehicle,Vehicle,Cardholder Authorised\nL19MXB,OTHER,Yes\n", "one Vehicle column"),
            (b"Plate,Vehicle,Cardholder Authorised\nL19MXB,L19MXB,Yes\n", "one Vehicle column"),
            (b"Vehicle,Cardholder Authorised\nL19MXB,Yes\nNEW001,Maybe\n", "Row 3.*Yes or No"),
            (b"Vehicle,Cardholder Authorised\nL19MXB,NEW001,Yes\n", "Quote fields containing commas"),
            (b"Vehicle,Cardholder Authorised\nL19MXB,No\n", "No authorised registrations"),
            (b'Vehicle,Cardholder Authorised\n"!,",Yes\n', "No authorised registrations"),
        ):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, message):
                parse_plate_import(source, "csv")

    def test_cardholder_excel_text_preserves_leading_zeros_and_rejects_numeric_cells(self):
        heading = '<c r="C1" t="inlineStr"><is><t>Cardholder Authorised</t></is></c>'
        status = '<c r="C2" t="inlineStr"><is><t>Yes</t></is></c>'
        imported = parse_plate_import(workbook('<c r="B2" t="s"><v>1</v></c>' + status,
                                               heading="Vehicle", extra_headings=heading), "xlsx")
        self.assertEqual(imported.plates, {"0012": "0012"})
        with self.assertRaisesRegex(ValueError, "Row 2.*as text"):
            parse_plate_import(workbook('<c r="B2" t="n"><v>12</v></c>' + status,
                                        heading="Vehicle", extra_headings=heading), "xlsx")

    def test_cardholder_limits_include_duplicates_excluded_rows_and_skipped_entries(self):
        with patch("platelog.vehicle_import.MAX_PLATES", 3):
            for source in (
                b'Vehicle,Cardholder Authorised\n"L19MXB,L19MXB,L19MXB,L19MXB",Yes\n',
                b'Vehicle,Cardholder Authorised\nL19MXB,Yes\n"!,!,!",No\n',
                b'Vehicle,Cardholder Authorised\n"L19MXB,!,!,!",Yes\n',
                b'Vehicle,Cardholder Authorised\nL19MXB,Yes\nN1,No\nN2,No\nN3,No\n',
            ):
                with self.subTest(source=source), self.assertRaisesRegex(ValueError, "at most 3"):
                    parse_plate_import(source, "csv")
        imported = parse_plate_import(
            ("Vehicle,Cardholder Authorised\nL19MXB,Yes\n" + "!,Yes\n" * 25).encode(), "csv")
        self.assertEqual((imported.skipped_entries, len(imported.warnings)), (25, 20))
        self.assertTrue(all(len(warning) < 160 for warning in imported.warnings))

    def apply_file(self, source, filename="cardholders.csv"):
        imported = parse_plate_import(source, "csv")
        draft = self.store.registration_import(imported.plates, True)
        return self.store.registration_import(imported.plates, True, draft["revision"],
                                               imported=imported, source=filename)

    def test_full_skipped_history_paginates_and_survives_clean_import_and_restart(self):
        data = ("Vehicle,Cardholder Authorised\nL19MXB,Yes\n" + "BAD/PLATE,Yes\n" * 63).encode()
        imported = parse_plate_import(data, "csv")
        self.assertEqual((imported.skipped_entries, len(imported.skipped), len(imported.warnings)), (63, 63, 20))
        self.store.registration_import(imported.plates, True)
        self.assertEqual(self.store.registration_import_history(), [])
        result = self.apply_file(data)
        run_id = result["import_run"]
        self.assertEqual(self.store.registration_import_page(run_id)[0]["source"], "cardholders.csv")
        first = self.store.registration_import_page(run_id)
        second = self.store.registration_import_page(run_id, 999)
        self.assertEqual((len(first[1]), first[2], first[3]), (50, 1, 2))
        self.assertEqual((len(second[1]), second[2], second[3]), (13, 2, 2))
        self.assertEqual((first[1][0]["source_row"], second[1][-1]["source_row"]), (3, 65))
        self.assertEqual(second[1][-1]["vehicle"], "BAD/PLATE")
        self.apply_file(b"Plate\nL19MXB\n", "clean.csv")
        reopened = Store(self.store.database.parent)
        self.assertEqual(len(reopened.registration_import_history()), 2)
        self.assertEqual(len(reopened.registration_skipped_rows(run_id)), 63)
        self.assertEqual(reopened.registered_count(), 1)

    def test_skipped_rows_csv_groups_source_rows_and_protects_spreadsheet_values(self):
        result = self.apply_file(b'Vehicle,Cardholder Authorised\n" L19 MXB , BAD/ONE, BAD.TWO,",Yes\n=1+2,Yes\n')
        output = skipped_csv(self.store, result["import_run"])
        rows = list(csv.DictReader(StringIO(output.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Original Vehicle value"], " L19 MXB , BAD/ONE, BAD.TWO,")
        self.assertEqual(rows[1]["Original Vehicle value"], "'=1+2")
        self.assertIn("Entry 2:  BAD/ONE", rows[0]["Skipped entries"])
        self.assertIn("Entry 4: Empty registration.", rows[0]["Reasons"])
        self.assertEqual(self.store.registration_import_page(result["import_run"])[0]["summary"]["import_report"]["skipped_entries"], 4)

    def test_rejected_import_keeps_all_captured_details_and_the_current_registered_list(self):
        self.apply_file(b"Plate\nL19MXB\n", "initial.csv")
        with self.assertRaises(ImportRejected) as rejected:
            parse_plate_import(b'Vehicle,Cardholder Authorised\n"!,",Yes\n', "csv")
        imported = rejected.exception.imported
        self.assertEqual(imported.skipped_entries, 2)
        run_id = self.store.record_registration_failure("invalid.csv", "replace", str(rejected.exception), imported)
        run, rows, _, _ = self.store.registration_import_page(run_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["vehicle"], "!,")
        self.assertIn("No authorised registrations", run["error"])
        self.assertEqual(run["summary"]["total"], 1)
        self.assertEqual(self.store.registration_import({"l19mxb": "L19MXB"}, False)["existing"], 1)

    def test_import_history_and_registered_list_commit_together(self):
        imported = parse_plate_import(b'Vehicle,Cardholder Authorised\n"L19MXB,BAD/PLATE",Yes\n', "csv")
        with patch.object(self.store, "_record_registration_import", side_effect=RuntimeError("Synthetic storage failure")):
            with self.assertRaisesRegex(RuntimeError, "Synthetic storage failure"):
                self.store.registration_import(imported.plates, True, "", imported=imported)
        self.assertEqual(self.store.registered_count(), 0)
        self.assertEqual(self.store.registration_import_history(), [])

    def test_history_limit_removes_only_old_import_details(self):
        with patch("platelog.store.IMPORT_HISTORY_LIMIT", 3):
            old = self.apply_file(b'Vehicle,Cardholder Authorised\n"L19MXB,BAD/PLATE",Yes\n')
            for number in range(3):
                self.apply_file(b"Plate\nL19MXB\n", f"clean-{number}.csv")
        self.assertEqual(len(self.store.registration_import_history()), 3)
        with self.assertRaises(LookupError):
            self.store.registration_import_page(old["import_run"])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM registration_import_rows").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM registration_import_skips").fetchone()[0], 0)
        self.assertEqual(self.store.registered_count(), 1)

    def test_review_escapes_original_values_and_bounds_long_table_cells(self):
        value = 'L19MXB,<img src=x onerror=alert(1)>,' + 'X' * 1000 + ',!,!'
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("Vehicle", "Cardholder Authorised"))
        writer.writerow((value, "Yes"))
        result = self.apply_file(output.getvalue().encode())
        run_id = result["import_run"]
        content = review_content(self.store, run_id, 1, timezone.utc)
        self.assertNotIn("<img src=x", content)
        self.assertIn("&lt;img src=x", content)
        self.assertIn("shortened to 240 characters", content)
        rows = self.store.registration_import_page(run_id)[1]
        self.assertTrue(all(len(row["vehicle"]) <= 240 and len(row["entry"]) <= 240 for row in rows))
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM registration_import_rows WHERE run_id=?", (run_id,)).fetchone()[0], 1)
        exported = list(csv.DictReader(StringIO(skipped_csv(self.store, run_id).decode("utf-8-sig"))))
        self.assertEqual(exported[0]["Original Vehicle value"], value)

    def test_cardholder_import_changes_unregistered_reporting(self):
        imported = parse_plate_import(
            b'Vehicle,Cardholder Authorised\n"L19 MXB,ABC 123",Yes\nNO123,No\nBAD/PLATE,Yes\n', "csv")
        draft = self.store.registration_import(imported.plates, True)
        self.store.registration_import(imported.plates, True, draft["revision"])
        end = 1_800_000_000_000
        for counter, plate in enumerate(("L19MXB", "ABC123", "NO123", "BADPLATE"), 1):
            event = parse_event("application/json", json.dumps(payload(end - counter * 1000, counter, plate, "new")).encode())
            self.store.save("entry", event)
        rows, _, _ = self.store.passage_page("", "entry", 1, 10)
        self.assertEqual({row["plate"]: row["registered"] for row in rows},
                         {"L19MXB": True, "ABC123": True, "NO123": False, "BADPLATE": False})
        rendered = render_stats(self.store, end, "", timezone.utc)
        self.assertIn("data-total>4", rendered)
        self.assertIn("data-unregistered>2", rendered)

    def test_scheduled_cardholder_refresh_saves_review_and_keeps_good_list_on_error(self):
        source = self.store.database.with_name("cardholders.csv")
        midnight = datetime(2026, 11, 18, tzinfo=timezone.utc)
        source.write_bytes(b'Vehicle,Cardholder Authorised\n"L19 MXB,BAD/PLATE",Yes\nNO123,No\n')
        save_refresh_schedule(self.store, True, str(source), "01:15", now=midnight)
        due = midnight + timedelta(hours=1, minutes=15)
        refresh_due_registrations(self.store, timezone.utc, due)
        success = refresh_status(self.store)["last_success"]
        self.assertEqual(success["total"], 1)
        self.assertEqual((success["import_report"]["skipped_entries"], success["import_report"]["excluded_rows"]), (1, 1))
        self.assertIn("Row 2", success["import_report"]["warnings"][0])
        run, rows, _, _ = self.store.registration_import_page(success["import_run"])
        self.assertEqual(run["kind"], "scheduled")
        self.assertEqual(rows[0]["vehicle"], "L19 MXB,BAD/PLATE")
        self.assertEqual(refresh_status(Store(self.store.database.parent))["last_success"], success)
        source.write_bytes(b"Vehicle,Cardholder Authorised\nNEW001,Unknown\n")
        refresh_due_registrations(self.store, timezone.utc, due + timedelta(days=1))
        self.assertEqual(refresh_status(self.store)["last_success"], success)
        self.assertIn("Yes or No", refresh_status(self.store)["last_attempt"]["error"])
        self.assertEqual(self.store.registration_import({"l19mxb": "L19MXB"}, False)["existing"], 1)
        self.assertEqual(self.store.registered_count(), 1)

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
            return PlateImport(plates={"new001": "NEW001"})

        with patch("platelog.registered_refresh.read_plate_source", side_effect=disable_during_read):
            refresh_due_registrations(self.store, timezone.utc, due)
        self.assertFalse(refresh_schedule(self.store)["enabled"])
        self.assertEqual(refresh_status(self.store), {})
        self.assertEqual(self.store.registration_import(original, False)["existing"], 1)
