"""Replace the registered list from a local or shared CSV on a daily schedule."""

import json
import os
import re
import stat
from datetime import datetime, time, timezone
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from .store import Store
from .vehicle_import import IMPORT_BYTES, parse_plates

DEFAULT_REFRESH = {"enabled": False, "source": "", "run_time": "01:15"}
_refresh_lock = Lock()


def refresh_schedule(store: Store) -> dict:
    saved = store.setting("registered_refresh_schedule")
    return json.loads(saved) if saved else DEFAULT_REFRESH.copy()


def refresh_status(store: Store) -> dict:
    saved = store.setting("registered_refresh_status")
    return json.loads(saved) if saved else {}


def csv_source(value: str) -> Path:
    path = Path(value)
    if (len(value) > 1024 or any(ord(char) < 32 for char in value)
            or value.replace("/", "\\").startswith(("\\\\.\\", "\\\\?\\"))
            or not path.is_absolute() or path.suffix.casefold() != ".csv"):
        raise ValueError("Enter the absolute path to a .csv file on the PlateLog server or a Windows UNC share.")
    return path


def save_refresh_schedule(store: Store, enabled: bool, source: str, run_time: str, *, now: datetime | None = None) -> None:
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", run_time):
        raise ValueError("Enter a refresh time between 00:00 and 23:59.")
    if enabled or source:
        source = str(csv_source(source))
    configured = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    store.save_registered_refresh_schedule({"enabled": enabled, "source": source, "run_time": run_time},
                                           configured.isoformat())


def read_plate_source(path: Path) -> dict[str, str]:
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("The CSV path must point to a regular file.")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(IMPORT_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > IMPORT_BYTES:
        raise ValueError("The CSV file exceeds the 4 MiB import limit.")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
        raise ValueError("The CSV file was changing during the read. Publish it after it finishes writing.")
    plates, _ = parse_plates(data, "csv")
    return plates


def refresh_due_registrations(store: Store, display_zone: timezone | ZoneInfo, now: datetime | None = None) -> None:
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        schedule = refresh_schedule(store)
        if not schedule["enabled"]:
            return
        saved_zone = store.setting("display_timezone")
        zone = ZoneInfo(saved_zone) if saved_zone else display_zone
        current = (now or datetime.now(timezone.utc)).astimezone(zone)
        due = datetime.combine(current.date(), time.fromisoformat(schedule["run_time"]), zone)
        configured = datetime.fromisoformat(schedule["configured_at"]).astimezone(zone)
        if current < due or configured > due:
            return
        day = current.date().isoformat()
        revision = schedule["revision"]
        attempt = refresh_status(store).get("last_attempt", {})
        if attempt.get("revision") == revision and attempt.get("day", "") >= day:
            return

        plates = None
        error = ""
        try:
            plates = read_plate_source(csv_source(schedule["source"]))
        except (FileNotFoundError, NotADirectoryError):
            error = "PlateLog could not find the CSV file."
        except PermissionError:
            error = "The PlateLog service could not read the CSV file. Check the file and share permissions."
        except OSError as exc:
            error = f"PlateLog could not read the CSV file. {exc.strerror or 'File access failed.'}"
        except ValueError as exc:
            error = str(exc)
        store.finish_registered_refresh(revision, day, current.isoformat(timespec="seconds"), plates, error)
    finally:
        _refresh_lock.release()
