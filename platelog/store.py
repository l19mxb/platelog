"""Store camera packets and their evidence in one SQLite transaction."""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .axis import Event, plate_key

TRACK_WINDOW_MS = 60_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS passages (
    id TEXT PRIMARY KEY,
    camera TEXT NOT NULL,
    profile TEXT NOT NULL,
    area TEXT NOT NULL,
    runtime_id TEXT NOT NULL,
    first_ms INTEGER NOT NULL,
    last_ms INTEGER NOT NULL,
    has_new INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0,
    closing_counter INTEGER,
    review INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS passage_track ON passages(camera, profile, area, runtime_id, first_ms);
CREATE INDEX IF NOT EXISTS passage_order ON passages(first_ms DESC, id DESC);
CREATE TABLE IF NOT EXISTS packets (
    id TEXT PRIMARY KEY,
    passage_id TEXT NOT NULL REFERENCES passages(id),
    camera TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    capture_ms INTEGER NOT NULL,
    received_ms INTEGER NOT NULL,
    counter INTEGER,
    plate TEXT NOT NULL,
    plate_key TEXT NOT NULL,
    direction TEXT NOT NULL,
    state TEXT NOT NULL,
    metadata TEXT NOT NULL,
    UNIQUE(camera, fingerprint)
);
CREATE INDEX IF NOT EXISTS packet_passage ON packets(passage_id, capture_ms DESC, counter DESC);
CREATE INDEX IF NOT EXISTS packet_plate ON packets(plate_key);
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    packet_id TEXT NOT NULL REFERENCES packets(id),
    position INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS image_packet ON images(packet_id, position);
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY,
    at_ms INTEGER NOT NULL,
    activity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expired_photos (passage_id TEXT PRIMARY KEY REFERENCES passages(id));
"""

PACKET_ORDER = "capture_ms DESC, counter DESC, received_ms DESC, rowid DESC"
DEFAULT_RETENTION = {"enabled": False, "passage_days": 30, "photo_days": 7}


def vehicle_details(packets: list[sqlite3.Row]) -> dict[str, str | None]:
    details: dict[str, str | None] = dict.fromkeys(("make", "model", "colour"))
    for packet in packets:
        vehicle = json.loads(packet["metadata"]).get("vehicle_info")
        if not isinstance(vehicle, dict):
            continue
        values = {key.lower(): value for key, value in vehicle.items()}
        for field, keys in (("make", ("make",)), ("model", ("model",)), ("colour", ("colour", "color"))):
            if details[field] is not None:
                continue
            for key in keys:
                value = values.get(key)
                if isinstance(value, str):
                    value = " ".join(value.split())[:80]
                    if value and value.casefold() not in {"unknown", "none", "n/a", "-"}:
                        details[field] = value.capitalize() if field == "colour" and value.isupper() else value
                        break
        if all(details.values()):
            break
    return details


class Store:
    def __init__(self, directory: Path):
        self.images = directory / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.database = directory / "platelog.sqlite3"
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def setting(self, name: str) -> str | None:
        with closing(self.connect()) as db:
            value = db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return value[0] if value else None

    def set_setting(self, name: str, value: str, activity: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))
            self._audit(db, activity)

    def change_admin_password(self, current: str, replacement: str) -> bool:
        with closing(self.connect()) as db, db:
            changed = db.execute("UPDATE settings SET value=? WHERE name='admin_password' AND value=?",
                                 (replacement, current))
            if not changed.rowcount:
                return False
            self._audit(db, "Administrator password changed")
            return True

    def _audit(self, db: sqlite3.Connection, activity: str) -> None:
        db.execute("INSERT INTO admin_audit(at_ms,activity) VALUES (?,?)", (time.time_ns() // 1_000_000, activity))
        db.execute("DELETE FROM admin_audit WHERE id <= (SELECT max(id)-1000 FROM admin_audit)")

    def record_admin_activity(self, activity: str) -> None:
        with closing(self.connect()) as db, db:
            self._audit(db, activity)

    def retention(self) -> dict:
        saved = self.setting("retention")
        return json.loads(saved) if saved else DEFAULT_RETENTION.copy()

    def admin_overview(self) -> tuple[dict, list[sqlite3.Row]]:
        with closing(self.connect()) as db:
            counts = db.execute("""SELECT (SELECT count(*) FROM passages) AS passages,
                                count(*) AS photos, coalesce(sum(byte_count),0) AS photo_bytes FROM images""").fetchone()
            audit = db.execute("SELECT at_ms,activity FROM admin_audit ORDER BY id DESC LIMIT 10").fetchall()
        return dict(counts), audit

    def apply_retention(self, now_ms: int | None = None) -> tuple[int, int]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT value FROM settings WHERE name='retention'").fetchone()
            config = json.loads(saved[0]) if saved else DEFAULT_RETENTION
            if not config["enabled"]:
                return 0, 0
            passage_before = now - config["passage_days"] * 86_400_000
            photo_before = now - config["photo_days"] * 86_400_000
            db.execute("""INSERT OR IGNORE INTO expired_photos
                          SELECT DISTINCT packets.passage_id FROM images JOIN packets ON packets.id=images.packet_id
                          JOIN passages ON passages.id=packets.passage_id WHERE packets.capture_ms<? AND passages.last_ms>=?""",
                       (photo_before, passage_before))
            photos = db.execute("""DELETE FROM images WHERE packet_id IN (
                                SELECT packets.id FROM packets JOIN passages ON passages.id=packets.passage_id
                                WHERE packets.capture_ms<? OR passages.last_ms<?)""", (photo_before, passage_before)).rowcount
            db.execute("DELETE FROM expired_photos WHERE passage_id IN (SELECT id FROM passages WHERE last_ms<?)", (passage_before,))
            db.execute("DELETE FROM packets WHERE passage_id IN (SELECT id FROM passages WHERE last_ms<?)", (passage_before,))
            passages = db.execute("DELETE FROM passages WHERE last_ms<?", (passage_before,)).rowcount
            db.execute("INSERT INTO settings VALUES ('retention_last_run',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (str(now),))
            if photos or passages:
                self._audit(db, f"Retention removed {passages} passages and {photos} photos")
        self.reconcile()
        return passages, photos

    def _passage(self, db: sqlite3.Connection, camera: str, event: Event) -> str:
        matches = []
        if event.runtime_id:
            candidates = db.execute(
                """SELECT * FROM passages WHERE camera=? AND profile=? AND area=? AND runtime_id=?
                   AND first_ms BETWEEN ? AND ?""",
                (camera, event.profile, event.area, event.runtime_id,
                 event.capture_ms - TRACK_WINDOW_MS, event.capture_ms + TRACK_WINDOW_MS),
            )
            for row in candidates:
                if event.state == "new":
                    fits = not row["has_new"] and event.capture_ms <= row["first_ms"]
                elif row["closed"]:
                    # Lost can repeat the initial frame timestamp. A lower packet
                    # counter can still identify a delayed update on that track.
                    closing_counter = row["closing_counter"]
                    if row["first_ms"] <= event.capture_ms <= row["last_ms"]:
                        fits = True
                    elif event.counter is not None and closing_counter is not None:
                        first_counter = db.execute(
                            "SELECT min(counter) FROM packets WHERE passage_id=? AND state='new'", (row["id"],),
                        ).fetchone()[0]
                        fits = (event.capture_ms > row["last_ms"] and first_counter is not None and
                                first_counter <= event.counter < closing_counter)
                    else:
                        fits = False
                else:
                    fits = not row["has_new"] or row["first_ms"] <= event.capture_ms
                if fits:
                    matches.append(row["id"])

        if len(matches) == 1:
            return matches[0]
        passage = uuid4().hex
        db.execute(
            """INSERT INTO passages(id,camera,profile,area,runtime_id,first_ms,last_ms,review)
               VALUES (?,?,?,?,?,?,?,?)""",
            (passage, camera, event.profile, event.area, event.runtime_id, event.capture_ms,
             event.capture_ms, int(len(matches) > 1)),
        )
        return passage

    def save(self, camera: str, event: Event) -> dict:
        try:
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                found = db.execute(
                    "SELECT id, passage_id FROM packets WHERE camera=? AND fingerprint=?",
                    (camera, event.fingerprint),
                ).fetchone()
                if found:
                    return {"packet_id": found["id"], "passage_id": found["passage_id"], "duplicate": True}
                passage = self._passage(db, camera, event)
                packet = uuid4().hex
                db.execute(
                    """INSERT INTO packets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (packet, passage, camera, event.fingerprint, event.capture_ms,
                     time.time_ns() // 1_000_000, event.counter, event.plate, plate_key(event.plate),
                     event.direction, event.state, event.metadata),
                )
                for position, data in enumerate(event.images):
                    image_id = uuid4().hex
                    path = self.images / f"{image_id}.jpg"
                    with path.open("xb") as image_file:
                        image_file.write(data)
                        image_file.flush()
                        os.fsync(image_file.fileno())
                    db.execute("INSERT INTO images VALUES (?,?,?,?,?)",
                               (image_id, packet, position, hashlib.sha256(data).hexdigest(), len(data)))
                if event.images and os.name == "posix":
                    directory = os.open(self.images, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                db.execute(
                    """UPDATE passages SET first_ms=min(first_ms,?), last_ms=max(last_ms,?),
                       has_new=max(has_new,?), closed=max(closed,?),
                       closing_counter=coalesce(?, closing_counter) WHERE id=?""",
                    (event.capture_ms, event.capture_ms, int(event.state == "new"),
                     int(event.state == "lost"), event.counter if event.state == "lost" else None, passage),
                )
            return {"packet_id": packet, "passage_id": passage, "duplicate": False}
        except (OSError, sqlite3.Error):
            # Reconciliation takes a writer lock and checks committed references before deleting.
            # If storage is still unavailable, the original error takes precedence.
            try:
                self.reconcile()
            except (OSError, sqlite3.Error):
                pass
            raise

    def reconcile(self) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for path in self.images.glob("*.jpg"):
                if len(path.stem) != 32 or any(char not in "0123456789abcdef" for char in path.stem):
                    continue
                if not db.execute("SELECT 1 FROM images WHERE id=?", (path.stem,)).fetchone():
                    path.unlink()

    def _summary(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        packets = db.execute(
            f"SELECT plate,direction,metadata FROM packets WHERE passage_id=? ORDER BY {PACKET_ORDER}",
            (row["id"],),
        ).fetchall()
        photos = db.execute(
            """SELECT images.id FROM packets JOIN images ON images.packet_id=packets.id WHERE passage_id=?
               ORDER BY capture_ms DESC,counter DESC,received_ms DESC,packets.rowid DESC,images.position""",
            (row["id"],),
        ).fetchall()
        direction = next((packet["direction"] for packet in packets if packet["direction"] != "unknown"), "unknown")
        return {"id": row["id"], "camera": row["camera"], "capture_ms": row["first_ms"],
                "plate": packets[0]["plate"], "direction": direction,
                "closed": bool(row["closed"]), "review": bool(row["review"]),
                "packets": len(packets), "images": len(photos), "photo_id": photos[0]["id"] if photos else None,
                "photo_expired": not photos and db.execute("SELECT 1 FROM expired_photos WHERE passage_id=?", (row["id"],)).fetchone() is not None,
                **vehicle_details(packets)}

    def passages(self, plate: str, camera: str, limit: int, *, partial_plate: bool = False,
                 start_ms: int | None = None, end_ms: int | None = None) -> list[dict]:
        key = plate_key(plate)
        if plate and not key:
            return []
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            rows = db.execute(
                """SELECT * FROM passages p WHERE p.first_ms BETWEEN ? AND ?
                   AND (?='' OR p.camera=?) AND (?='' OR EXISTS (
                       SELECT 1 FROM packets r WHERE r.passage_id=p.id
                       AND (r.plate_key=? OR (? AND instr(r.plate_key,?)>0))))
                   ORDER BY p.first_ms DESC,p.id DESC LIMIT ?""",
                (start_ms if start_ms is not None else 0, end_ms - 1 if end_ms is not None else 253402300799999,
                 camera, camera, key, key, partial_plate, key, limit),
            ).fetchall()
            return [self._summary(db, row) for row in rows]

    def detail(self, passage: str) -> dict | None:
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM passages WHERE id=?", (passage,)).fetchone()
            if row is None:
                return None
            history = []
            packets = db.execute(
                f"""SELECT id,capture_ms,received_ms,counter,plate,direction,state FROM packets
                    WHERE passage_id=? ORDER BY {PACKET_ORDER}""", (passage,),
            )
            for packet in packets:
                images = db.execute(
                    "SELECT id,sha256,byte_count FROM images WHERE packet_id=? ORDER BY position",
                    (packet["id"],),
                )
                history.append({**dict(packet), "images": [dict(image) for image in images]})
            return {**self._summary(db, row), "history": history}

    def image_path(self, image_id: str) -> Path | None:
        with closing(self.connect()) as db:
            if not db.execute("SELECT 1 FROM images WHERE id=?", (image_id,)).fetchone():
                return None
        path = self.images / f"{image_id}.jpg"
        return path if path.is_file() else None
