import base64
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from platelog.axis import PayloadError, parse_event
from platelog.simulator import demo_jpeg, multipart, payload
from platelog.store import Store


def event(body):
    return parse_event("application/json", json.dumps(body).encode())


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = Store(self.directory)
        self.start = 1_800_000_000_000
        self.photo = demo_jpeg("DEMO-A01")
        self.first = payload(self.start, 8, "DEMO-AO1", "new", "out")

    def with_photo(self):
        return event({**self.first, "ImageArray": [{"BinaryImage": base64.b64encode(self.photo).decode()}]})

    def test_concurrent_replay_and_equivalent_multipart(self):
        incoming = self.with_photo()
        kind, body = multipart(self.first, self.photo)
        other = parse_event(kind, body)
        self.assertEqual(other.fingerprint, incoming.fingerprint)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda packet: self.store.save("entry", packet), [incoming, other] * 2))
        self.assertEqual(sum(not result["duplicate"] for result in results), 1)
        self.assertEqual(len({result["packet_id"] for result in results}), 1)
        self.assertEqual(len(list(self.store.images.iterdir())), 1)

    def test_disk_failure_rolls_back_and_retry_survives_reopen(self):
        incoming = self.with_photo()
        with patch("platelog.store.os.fsync", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                self.store.save("entry", incoming)
        self.assertEqual(self.store.passages("", "", 10), [])
        self.assertEqual(list(self.store.images.iterdir()), [])
        saved = self.store.save("entry", incoming)
        reopened = Store(self.directory)
        reopened.reconcile()
        detail = reopened.detail(saved["passage_id"])
        self.assertEqual(detail["direction"], "unknown")
        image = detail["history"][0]["images"][0]
        self.assertEqual(reopened.image_path(image["id"]).read_bytes(), self.photo)
        self.assertTrue(reopened.save("entry", incoming)["duplicate"])

    def test_new_after_closed_starts_another_passage(self):
        first = self.store.save("entry", event(self.first))
        self.store.save("entry", event(payload(self.start, 10, "DEMO-A01", "lost", "in")))
        late = self.store.save("entry", event(payload(self.start + 200, 9, "DEMO-A01", "update", "in")))
        second = self.store.save("entry", event(payload(self.start + 1000, 11, "DEMO-A01", "new")))
        self.assertEqual(first["passage_id"], late["passage_id"])
        self.assertNotEqual(first["passage_id"], second["passage_id"])

    def test_same_counter_can_carry_changed_information(self):
        first = self.store.save("entry", event(self.first))
        correction = self.store.save("entry", event(payload(self.start + 200, 8, "DEMO-A01", "update", "in")))
        self.assertFalse(correction["duplicate"])
        self.assertEqual(first["passage_id"], correction["passage_id"])
        self.assertEqual(self.store.detail(first["passage_id"])["plate"], "DEMO-A01")

    def test_bad_jpeg_rejected_and_inline_bytes_removed(self):
        with self.assertRaisesRegex(PayloadError, "Invalid JPEG"):
            event({**self.first, "ImageArray": [{"BinaryImage": "ZmFrZQ=="}]})
        self.assertNotIn("ImageArray", json.loads(self.with_photo().metadata))

    def test_reconcile_removes_only_unreferenced_managed_images(self):
        self.store.save("entry", self.with_photo())
        orphan = self.store.images / ("0" * 32 + ".jpg")
        foreign = self.store.images / "unmanaged.jpg"
        orphan.write_bytes(self.photo)
        foreign.write_bytes(self.photo)
        self.store.reconcile()
        self.assertFalse(orphan.exists())
        self.assertTrue(foreign.exists())
        with closing(self.store.connect()) as db:
            saved = db.execute("SELECT id FROM images").fetchone()[0]
        self.assertTrue(self.store.image_path(saved).is_file())
