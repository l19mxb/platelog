"""Store camera packets and their evidence in one SQLite transaction."""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .alarms import MAX_LIVE_AGE_MS, MAX_LIVE_FUTURE_MS, SETTLE_MS, AlarmRule
from .axis import Event, plate_key

TRACK_WINDOW_MS = 60_000
DEFAULT_RETENTION = {"enabled": True, "passage_days": 360, "photo_days": 90}
LEGACY_RETENTION = {**DEFAULT_RETENTION, "enabled": False}

SCHEMA = f"""
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings (name, value)
    SELECT 'retention', '{json.dumps(DEFAULT_RETENTION)}'
    WHERE NOT EXISTS (SELECT 1 FROM sqlite_schema WHERE type='table' AND name='passages');
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
CREATE TABLE IF NOT EXISTS camera_settings (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('disabled', 'none', 'basic')),
    username TEXT NOT NULL,
    verifier TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY,
    at_ms INTEGER NOT NULL,
    activity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expired_photos (passage_id TEXT PRIMARY KEY REFERENCES passages(id));
CREATE TABLE IF NOT EXISTS registered_plates (plate_key TEXT PRIMARY KEY, plate TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alarm_rules (
    camera TEXT PRIMARY KEY REFERENCES camera_settings(id),
    mode TEXT NOT NULL CHECK (mode IN ('off', 'log', 'live')),
    plate_status TEXT NOT NULL CHECK (plate_status IN ('registered', 'unregistered')),
    module TEXT NOT NULL,
    virtual_input INTEGER NOT NULL,
    pulse_seconds INTEGER NOT NULL,
    username TEXT NOT NULL,
    secret_name TEXT NOT NULL,
    revision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alarm_runs (
    passage_id TEXT PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
    camera TEXT NOT NULL REFERENCES alarm_rules(camera),
    revision TEXT NOT NULL,
    due_ms INTEGER NOT NULL,
    plate TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT 'pending',
    result_ms INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS alarm_pending ON alarm_runs(due_ms) WHERE result='pending';
CREATE INDEX IF NOT EXISTS alarm_activity ON alarm_runs(result_ms DESC) WHERE result_ms IS NOT NULL;
COMMIT;
"""

PACKET_ORDER = "capture_ms DESC, counter DESC, received_ms DESC, rowid DESC"
PASSAGE_MATCH = """FROM passages p WHERE p.first_ms BETWEEN ? AND ?
    AND (?='' OR p.camera=?) AND (?='' OR EXISTS (
        SELECT 1 FROM packets r WHERE r.passage_id=p.id
        AND (r.plate_key=? OR (? AND instr(r.plate_key,?)>0))))"""


def passage_match_args(plate: str, camera: str, partial_plate: bool, start_ms: int | None, end_ms: int | None):
    key = plate_key(plate)
    if plate and not key:
        return None
    return (start_ms if start_ms is not None else 0, end_ms - 1 if end_ms is not None else 253402300799999,
            camera, camera, key, key, partial_plate, key)
DEFAULT_REGISTER_ACCESS = {"required": False, "verifier": "", "revision": ""}


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


class RegistrationChanged(ValueError):
    """The registered list changed after the import preview."""


class Store:
    def __init__(self, directory: Path):
        self.images = directory / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.database = directory / "platelog.sqlite3"
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            columns = {column["name"] for column in db.execute("PRAGMA table_info(alarm_rules)")}
            if "plate_status" not in columns:
                db.execute("""ALTER TABLE alarm_rules ADD COLUMN plate_status TEXT NOT NULL DEFAULT 'unregistered'
                              CHECK (plate_status IN ('registered', 'unregistered'))""")
            if "username" not in columns:
                db.execute("ALTER TABLE alarm_rules ADD COLUMN username TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE alarm_rules ADD COLUMN secret_name TEXT NOT NULL DEFAULT ''")
                if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='alarm_devices'").fetchone():
                    db.execute("""UPDATE alarm_rules AS r SET (username, secret_name)=(
                                     SELECT d.username,d.secret_name FROM alarm_devices d WHERE d.module=r.module)
                                  WHERE EXISTS (SELECT 1 FROM alarm_devices d WHERE d.module=r.module)""")

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

    def initialize_admin_password(self, verifier: str) -> None:
        with closing(self.connect()) as db, db:
            inserted = db.execute("INSERT OR IGNORE INTO settings VALUES ('admin_password', ?)", (verifier,))
            if inserted.rowcount:
                self._audit(db, "Initial administrator password enabled")

    def register_access(self) -> dict:
        saved = self.setting("register_access")
        return json.loads(saved) if saved else DEFAULT_REGISTER_ACCESS.copy()

    def set_register_access(self, required: bool, verifier: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT value FROM settings WHERE name='register_access'").fetchone()
            current = json.loads(saved[0]) if saved else DEFAULT_REGISTER_ACCESS
            verifier = verifier or current["verifier"]
            if required and not verifier:
                raise ValueError("Set a staff password before requiring sign-in.")
            if saved and (required, verifier) == (current["required"], current["verifier"]) and "staff_admin" not in current:
                return
            value = json.dumps({"required": required, "verifier": verifier, "revision": uuid4().hex})
            db.execute("INSERT INTO settings VALUES ('register_access', ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                       (value,))
            self._audit(db, f"Register sign-in {'required' if required else 'optional'}.")

    def _audit(self, db: sqlite3.Connection, activity: str) -> None:
        db.execute("INSERT INTO admin_audit(at_ms,activity) VALUES (?,?)", (time.time_ns() // 1_000_000, activity))
        db.execute("DELETE FROM admin_audit WHERE id <= (SELECT max(id)-1000 FROM admin_audit)")

    def record_admin_activity(self, activity: str) -> None:
        with closing(self.connect()) as db, db:
            self._audit(db, activity)

    def retention(self) -> dict:
        saved = self.setting("retention")
        return json.loads(saved) if saved else LEGACY_RETENTION.copy()

    def last_received_ms(self) -> int | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT received_ms FROM packets ORDER BY rowid DESC LIMIT 1").fetchone()
            return row[0] if row else None

    def cameras(self) -> list[sqlite3.Row]:
        with closing(self.connect()) as db:
            return db.execute("SELECT id, mode, username FROM camera_settings ORDER BY id").fetchall()

    def camera(self, camera_id: str) -> sqlite3.Row | None:
        with closing(self.connect()) as db:
            return db.execute("SELECT mode, username, verifier FROM camera_settings WHERE id=?", (camera_id,)).fetchone()

    def save_camera(self, camera_id: str, mode: str, username: str, verifier: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT username, verifier FROM camera_settings WHERE id=?", (camera_id,)).fetchone()
            if mode == "basic":
                verifier = verifier or (current["verifier"] if current and current["username"] == username else "")
                if not verifier:
                    raise ValueError("Set a password for this camera username.")
            elif current:
                username, verifier = current["username"], current["verifier"]
            db.execute("""INSERT INTO camera_settings VALUES (?, ?, ?, ?)
                          ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, username=excluded.username,
                          verifier=excluded.verifier""", (camera_id, mode, username, verifier))
            self._audit(db, f"Camera {camera_id} configured ({mode})")

    def passage_cameras(self) -> set[str]:
        with closing(self.connect()) as db:
            return {row[0] for row in db.execute("SELECT DISTINCT camera FROM passages")}

    def alarm_overview(self) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        with closing(self.connect()) as db:
            rules = db.execute("SELECT * FROM alarm_rules ORDER BY camera").fetchall()
            recent = db.execute("""SELECT passage_id, camera, plate, result, result_ms, detail FROM alarm_runs
                                 WHERE result_ms IS NOT NULL ORDER BY result_ms DESC, rowid DESC LIMIT 12""").fetchall()
        return rules, recent

    def save_alarm_rule(self, camera: str, rule: AlarmRule) -> None:
        values = (rule.mode, rule.trigger, rule.module, rule.virtual_input, rule.pulse_seconds,
                  rule.username, rule.secret_name)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM camera_settings WHERE id=?", (camera,)).fetchone():
                raise ValueError("Choose a configured Axis source camera.")
            if rule.mode == "live" and not (rule.module and rule.username and os.environ.get(rule.secret_name)):
                raise ValueError("Live mode needs a timed HTTPS command, an Axis username, and a password variable set for the PlateLog service.")
            current = db.execute("""SELECT mode, plate_status, module, virtual_input, pulse_seconds, username, secret_name
                                  FROM alarm_rules WHERE camera=?""", (camera,)).fetchone()
            if current is not None and tuple(current) == values:
                return
            db.execute("""INSERT INTO alarm_rules
                          (camera, mode, plate_status, module, virtual_input, pulse_seconds, username, secret_name, revision)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                          ON CONFLICT(camera) DO UPDATE SET mode=excluded.mode, plate_status=excluded.plate_status, module=excluded.module,
                          virtual_input=excluded.virtual_input, pulse_seconds=excluded.pulse_seconds,
                          username=excluded.username, secret_name=excluded.secret_name,
                          revision=excluded.revision""",
                       (camera, *values, uuid4().hex))
            db.execute("""UPDATE alarm_runs SET result='held', result_ms=?, detail='Alarm rule changed.'
                          WHERE camera=? AND result='pending'""", (time.time_ns() // 1_000_000, camera))
            self._audit(db, f"Alarm rule for {camera} saved ({rule.mode})")

    def claim_alarm(self, now_ms: int) -> dict | None:
        due = "result='pending' AND due_ms<=?"
        with closing(self.connect()) as db, db:
            if not db.execute(f"SELECT 1 FROM alarm_runs WHERE {due} LIMIT 1", (now_ms,)).fetchone():
                return None
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(f"""SELECT a.*, r.mode, r.plate_status, r.module, r.virtual_input, r.pulse_seconds, r.username, r.secret_name,
                                     r.revision AS rule_revision, c.mode AS source_mode, p.review
                                  FROM (SELECT * FROM alarm_runs WHERE {due} ORDER BY due_ms, rowid LIMIT 1) a
                                  JOIN alarm_rules r ON r.camera=a.camera JOIN camera_settings c ON c.id=a.camera
                                  JOIN passages p ON p.id=a.passage_id""", (now_ms,)).fetchone()
            if run is None:
                return None
            latest = db.execute(f"""SELECT plate, capture_ms, EXISTS (
                                        SELECT 1 FROM registered_plates r WHERE r.plate_key=p.plate_key
                                    ) AS registered FROM packets p WHERE passage_id=?
                                    ORDER BY {PACKET_ORDER} LIMIT 1""", (run["passage_id"],)).fetchone()
            if run["revision"] != run["rule_revision"] or run["mode"] == "off" or run["source_mode"] == "disabled":
                result, detail = "held", "Alarm rule or source camera is disabled or changed."
            elif not db.execute("SELECT 1 FROM registered_plates LIMIT 1").fetchone():
                result, detail = "held", "Load a registered vehicle list before evaluating alarms."
            elif run["review"]:
                result, detail = "held", "The camera packets could not be matched to one passage."
            elif bool(latest["registered"]) != (run["plate_status"] == "registered"):
                result, detail = ("registered", "Plate matches the registered list.") if latest["registered"] else (
                    "unregistered", "Plate is absent from the registered list.")
            elif run["mode"] == "log":
                result = "logged"
                detail = f'{run["plate_status"].capitalize()} plate matched. A pulse would be requested in Live mode.'
            elif not (run["username"] and run["secret_name"]):
                result, detail = "held", "Set the Axis username and password variable for this rule."
            elif not -MAX_LIVE_FUTURE_MS <= now_ms - latest["capture_ms"] <= MAX_LIVE_AGE_MS:
                result, detail = "held", "The plate capture is outside the live alarm window."
            else:
                result, detail = "dispatching", "A VAPIX request was claimed. Axis has yet to confirm it."
            db.execute("UPDATE alarm_runs SET plate=?, result=?, result_ms=?, detail=? WHERE passage_id=?",
                       (latest["plate"], result, now_ms, detail, run["passage_id"]))
            return {**dict(run), "result": result}

    def finish_alarm(self, passage: str, result: str, detail: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE alarm_runs SET result=?, detail=?
                          WHERE passage_id=? AND result='dispatching'""", (result, detail, passage))

    def registered_count(self) -> int:
        with closing(self.connect()) as db:
            return db.execute("SELECT count(*) FROM registered_plates").fetchone()[0]

    def registration_import(self, plates: dict[str, str], replace: bool,
                            expected_revision: str | None = None) -> dict:
        """Preview changes or apply them atomically against the reviewed revision."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN" if expected_revision is None else "BEGIN IMMEDIATE")
            applying = expected_revision is not None
            result = self._registration_changes(db, plates, replace, apply=applying, expected_revision=expected_revision)
            if applying and (result["added"] or result["removed"]):
                mode = "replaced" if replace else "imported"
                self._audit(db, f"Registered list {mode}: {result['added']} added, {result['removed']} removed")
            return result

    def _registration_changes(self, db: sqlite3.Connection, plates: dict[str, str], replace: bool,
                              *, apply: bool, expected_revision: str | None = None) -> dict:
        saved = db.execute("SELECT value FROM settings WHERE name='registered_plates_revision'").fetchone()
        revision = saved[0] if saved else ""
        if expected_revision is not None and expected_revision != revision:
            raise RegistrationChanged("The registered list changed. Preview the file again before importing.")

        current = dict(db.execute("SELECT plate_key, plate FROM registered_plates"))
        added = plates.keys() - current.keys()
        removed = current.keys() - plates.keys() if replace else set()
        total = len(current) + len(added) - len(removed)
        if total > 50_000:
            raise ValueError("The registered list can hold up to 50,000 plates.")
        if apply and (added or removed):
            db.executemany("DELETE FROM registered_plates WHERE plate_key=?", ((key,) for key in removed))
            db.executemany("INSERT INTO registered_plates VALUES (?,?)", ((key, plates[key]) for key in added))
            db.execute("""INSERT INTO settings VALUES ('registered_plates_revision',?)
                          ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (uuid4().hex,))
        return {"revision": revision, "total": total, "added": len(added), "removed": len(removed),
                "existing": len(plates) - len(added),
                "sample": [{"plate": plates[key], "action": "Add" if key in added else "Keep"}
                           for key in list(plates)[:20]],
                "removed_sample": [current[key] for key in sorted(removed)[:20]]}

    def save_registered_refresh_schedule(self, config: dict, configured_at: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE name='registered_refresh_schedule'").fetchone()
            if row:
                current = json.loads(row[0])
                if all(current[key] == value for key, value in config.items()):
                    return
            schedule = json.dumps({**config, "revision": uuid4().hex, "configured_at": configured_at})
            db.execute("""INSERT INTO settings VALUES ('registered_refresh_schedule',?)
                          ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (schedule,))
            activity = "Scheduled registered list refresh enabled" if config["enabled"] else "Scheduled registered list refresh disabled"
            self._audit(db, activity)

    def finish_registered_refresh(self, revision: str, day: str, attempted_at: str,
                                  plates: dict[str, str] | None, error: str = "") -> None:
        """Commit the replacement and its status together if the schedule is still current."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            schedule = db.execute("SELECT value FROM settings WHERE name='registered_refresh_schedule'").fetchone()
            if not schedule or json.loads(schedule[0])["revision"] != revision:
                return
            saved = db.execute("SELECT value FROM settings WHERE name='registered_refresh_status'").fetchone()
            status = json.loads(saved[0]) if saved else {}
            attempt = status.get("last_attempt", {})
            if attempt.get("revision") == revision and attempt.get("day", "") >= day:
                return
            latest = {"revision": revision, "day": day, "attempted_at": attempted_at, "error": error}
            if plates:
                result = self._registration_changes(db, plates, True, apply=True)
                latest.update({key: result[key] for key in ("added", "removed", "total")})
                status["last_success"] = latest
                activity = f"Scheduled registered list refreshed: {result['added']} added, {result['removed']} removed"
            else:
                activity = f"Scheduled registered list refresh failed: {error}"
            status["last_attempt"] = latest
            db.execute("""INSERT INTO settings VALUES ('registered_refresh_status',?)
                          ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (json.dumps(status),))
            self._audit(db, activity)

    def passage_activity(self, start_ms: int, end_ms: int, camera: str) -> list[sqlite3.Row]:
        """Read one effective plate per passage from the same snapshot as its list match."""
        with closing(self.connect()) as db:
            return db.execute(f"""
                SELECT p.first_ms, latest.plate_key, r.plate_key IS NOT NULL AS registered
                FROM passages p
                JOIN packets latest ON latest.rowid=(
                    SELECT rowid FROM packets WHERE passage_id=p.id ORDER BY {PACKET_ORDER} LIMIT 1
                )
                LEFT JOIN registered_plates r ON r.plate_key=latest.plate_key
                WHERE p.first_ms>=? AND p.first_ms<? AND (?='' OR p.camera=?)
                ORDER BY p.first_ms, p.id
            """, (start_ms, end_ms, camera, camera)).fetchall()

    def admin_overview(self) -> tuple[dict, list[sqlite3.Row]]:
        with closing(self.connect()) as db:
            counts = db.execute("""SELECT (SELECT count(*) FROM passages) AS passages,
                                (SELECT count(*) FROM packets) AS packets,
                                count(*) AS photos, coalesce(sum(byte_count),0) AS photo_bytes FROM images""").fetchone()
            audit = db.execute("SELECT at_ms,activity FROM admin_audit ORDER BY id DESC LIMIT 10").fetchall()
        return dict(counts), audit

    def apply_retention(self, now_ms: int | None = None, *, confirmed_limits: tuple[int, int] | None = None) -> tuple[int, int]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT value FROM settings WHERE name='retention'").fetchone()
            config = json.loads(saved[0]) if saved else LEGACY_RETENTION
            manual = confirmed_limits is not None
            if manual and confirmed_limits != (config["passage_days"], config["photo_days"]):
                raise ValueError("Retention limits changed. Review the saved limits before purging.")
            if not config["enabled"] and not manual:
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
            if manual or photos or passages:
                action = "Manual purge" if manual else "Retention"
                self._audit(db, f"{action} removed {passages} passages and {photos} photos")
        self.reconcile()
        return passages, photos

    def _passage(self, db: sqlite3.Connection, camera: str, event: Event) -> tuple[str, bool]:
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
            return matches[0], False
        passage = uuid4().hex
        db.execute(
            """INSERT INTO passages(id,camera,profile,area,runtime_id,first_ms,last_ms,review)
               VALUES (?,?,?,?,?,?,?,?)""",
            (passage, camera, event.profile, event.area, event.runtime_id, event.capture_ms,
             event.capture_ms, int(len(matches) > 1)),
        )
        return passage, True

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
                passage, new_passage = self._passage(db, camera, event)
                packet = uuid4().hex
                received_ms = time.time_ns() // 1_000_000
                db.execute(
                    """INSERT INTO packets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (packet, passage, camera, event.fingerprint, event.capture_ms, received_ms,
                     event.counter, event.plate, plate_key(event.plate),
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
                due_ms = received_ms + SETTLE_MS
                if new_passage:
                    db.execute("""INSERT INTO alarm_runs (passage_id, camera, revision, due_ms, plate)
                                  SELECT ?, camera, revision, ?, ? FROM alarm_rules WHERE camera=? AND mode!='off'""",
                               (passage, due_ms, event.plate, camera))
                else:
                    db.execute("UPDATE alarm_runs SET due_ms=? WHERE passage_id=? AND result='pending'",
                               (due_ms, passage))
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
        args = passage_match_args(plate, camera, partial_plate, start_ms, end_ms)
        if args is None:
            return []
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            return self._matching_passages(db, args, limit)

    def passage_page(self, plate: str, camera: str, page: int, size: int, *,
                     start_ms: int | None = None, end_ms: int | None = None) -> tuple[list[dict], int, int]:
        args = passage_match_args(plate, camera, True, start_ms, end_ms)
        if args is None:
            return [], 0, 1
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT COUNT(*) {PASSAGE_MATCH}", args).fetchone()[0]
            last = max(1, (total + size - 1) // size)
            page = min(page, last)
            rows = self._matching_passages(db, args, size, (page - 1) * size)
            if rows:
                keys = [plate_key(row["plate"]) for row in rows]
                placeholders = ",".join("?" for _ in keys)
                registered = {row[0] for row in db.execute(
                    f"SELECT plate_key FROM registered_plates WHERE plate_key IN ({placeholders})", keys)}
                for row, key in zip(rows, keys):
                    row["registered"] = key in registered
            return rows, total, page

    def export_passages(self, plate: str, camera: str, limit: int, *,
                        start_ms: int | None = None, end_ms: int | None = None) -> list[dict] | None:
        """Return a complete report snapshot, or None if it would exceed the limit."""
        args = passage_match_args(plate, camera, True, start_ms, end_ms)
        if args is None:
            return []
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            rows = self._passage_rows(db, args, limit + 1)
            if len(rows) > limit:
                return None
            return [self._summary(db, row) for row in rows]

    def _matching_passages(self, db: sqlite3.Connection, args: tuple, limit: int, offset: int = 0) -> list[dict]:
        return [self._summary(db, row) for row in self._passage_rows(db, args, limit, offset)]

    def _passage_rows(self, db: sqlite3.Connection, args: tuple, limit: int, offset: int = 0) -> list[sqlite3.Row]:
        return db.execute(f"SELECT p.* {PASSAGE_MATCH} ORDER BY p.first_ms DESC, p.id DESC LIMIT ? OFFSET ?",
                          (*args, limit, offset)).fetchall()

    def report_passages(self, start_ms: int, end_ms: int):
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT * FROM passages WHERE first_ms>=? AND first_ms<? ORDER BY first_ms, id",
                              (start_ms, end_ms))
            for row in rows:
                yield self._summary(db, row)

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
