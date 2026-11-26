"""Write the previous local calendar day's passages as a scheduled CSV."""

import csv
import json
import os
import stat
import tempfile
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from .store import Store
from .diagnostics import record_event


DEFAULT_SCHEDULE = {"enabled": False, "run_time": "00:05", "folder": ""}
HEADERS = ("Plate", "Capture time", "Camera", "Direction", "Make", "Model", "Colour",
           "Event count", "Photo count", "Passage ID")
_export_lock = Lock()
_folder_check_lock = Lock()


def report_schedule(store: Store) -> dict:
    saved = store.setting("daily_csv_schedule")
    return json.loads(saved) if saved else DEFAULT_SCHEDULE.copy()


def report_folder(value: str) -> Path:
    if len(value) > 1024:
        raise ValueError("Use a destination folder of at most 1024 characters.")
    if any(ord(character) < 32 for character in value) or value.replace("/", "\\").startswith(("\\\\.\\", "\\\\?\\")):
        raise ValueError("Enter an absolute report folder on the server, or a UNC share on Windows.")
    folder = Path(value)
    if not folder.is_absolute():
        raise ValueError("Enter an absolute report folder on the server, or a UNC share on Windows.")
    return folder


def check_report_folder(value: str) -> None:
    """Test the destination with the service's own file permissions."""
    folder = report_folder(value)
    if not _folder_check_lock.acquire(blocking=False):
        raise ValueError("A destination folder test is already running. Try again shortly.")
    try:
        _check_folder_access(folder)
    finally:
        _folder_check_lock.release()


def _check_folder_access(folder: Path) -> None:
    try:
        if not stat.S_ISDIR(folder.stat().st_mode):
            raise ValueError("The destination is a file. Choose an existing folder.")
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ValueError("The destination folder does not exist on the PlateLog server.") from error
    except OSError as error:
        raise ValueError(f"PlateLog could not access the destination folder. {error.strerror or error}.") from error

    probe = None
    sample = b"PlateLog destination folder check\n"
    action = "create or write a test file"
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=folder, prefix=".platelog-check-", suffix=".tmp", delete=False) as output:
            probe = Path(output.name)
            output.write(sample)
            output.flush()
            os.fsync(output.fileno())
        action = "read the test file back"
        with probe.open("rb") as input_file:
            if input_file.read(len(sample) + 1) != sample:
                raise ValueError("The test file contents did not match what PlateLog wrote.")
    except OSError as error:
        raise ValueError(f"PlateLog could not {action} in the destination folder. {error.strerror or error}.") from error
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError as error:
                raise ValueError(f"PlateLog could not remove its test file {probe.name}. {error.strerror or error}.") from error


def csv_text(value: str | None) -> str:
    text = value or ""
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")):
        return "'" + text
    return text


def write_report(store: Store, target: Path, start: datetime, end: datetime) -> int:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    count = 0
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=target.parent,
                                         prefix=".platelog-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            writer = csv.writer(output)
            writer.writerow(HEADERS)
            with closing(store.report_passages(start_ms, end_ms)) as passages:
                for row in passages:
                    captured = datetime.fromtimestamp(row["capture_ms"] / 1000, start.tzinfo)
                    writer.writerow((csv_text(row["plate"]), captured.isoformat(timespec="milliseconds"),
                                     csv_text(row["camera"]), row["direction"], csv_text(row["make"]),
                                     csv_text(row["model"]), csv_text(row["colour"]), row["packets"],
                                     row["images"], row["id"]))
                    count += 1
            output.flush()
            os.fsync(output.fileno())
        if os.name == "nt":
            temporary.rename(target)  # Windows rename fails if the report exists.
        else:
            if target.exists():
                raise FileExistsError(str(target))
            os.replace(temporary, target)
        return count
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_due_report(store: Store, display_zone: timezone | ZoneInfo, now: datetime | None = None) -> None:
    # A web listener restart can leave its previous export thread finishing a network write.
    if not _export_lock.acquire(blocking=False):
        return
    try:
        _export_due_report(store, display_zone, now)
    finally:
        _export_lock.release()


def _export_due_report(store: Store, display_zone: timezone | ZoneInfo, now: datetime | None) -> None:
    schedule = report_schedule(store)
    if not schedule["enabled"]:
        return
    saved_zone = store.setting("display_timezone")
    zone = ZoneInfo(saved_zone) if saved_zone else display_zone
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    if current.time() < time.fromisoformat(schedule["run_time"]):
        return
    day = current.date() - timedelta(days=1)
    target = report_folder(schedule["folder"]) / f"platelog-{day.isoformat()}.csv"
    saved = store.setting("daily_csv_status")
    last = json.loads(saved) if saved else {}
    if last.get("file") == str(target) and last.get("result") in {"written", "exists"}:
        return
    status = {"file": str(target), "date": day.isoformat(), "attempted_at": current.isoformat(timespec="seconds")}
    try:
        if target.is_file():
            status.update(result="exists", message="A report file already exists for this date.")
        else:
            start = datetime.combine(day, time.min, zone)
            end = datetime.combine(current.date(), time.min, zone)
            count = write_report(store, target, start, end)
            unit = "passage" if count == 1 else "passages"
            status.update(result="written", message=f"Wrote {count:,} {unit}.")
    except FileExistsError:
        status.update(result="exists", message="A report file already exists for this date.")
    except OSError as exc:
        record_event(store.database.parent, "csv.failed", "csv", level="error", error=exc)
        status.update(result="error", message=exc.strerror or str(exc))
    activity = f"Daily CSV for {day} · {status['message']}"
    if status["result"] == "error" and all(last.get(key) == status[key] for key in ("file", "result", "message")):
        return
    store.set_setting("daily_csv_status", json.dumps(status), activity)
    record_event(store.database.parent, "csv.completed", "csv", outcome=status["result"])
