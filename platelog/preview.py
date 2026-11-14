"""Browser view of the data written by the simulator."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections import Counter
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import parse_qs
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from PIL import Image
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .receiver import CAMERAS, ID_PATTERN
from .store import Store

CSS = """
:root { font: 16px/1.5 system-ui, sans-serif; color: #172638; background: #f4f7fa; }
body { margin: 0; }
main { padding: 24px clamp(12px, 2vw, 32px) 40px; }
h1, h2, p { margin: 0; } h1 { font-size: 2.2rem; letter-spacing: -.045em; }
header { margin-bottom: 28px; } header p, .muted, caption { color: #4b5d70; }
.tabs { display: flex; gap: 18px; margin: 14px 0 0; border-bottom: 1px solid #d3dce5; }
.tabs a { padding: 6px 2px; text-decoration: none; }
.tabs a[aria-current] { border-bottom: 2px solid #194b79; font-weight: 650; }
.admin { max-width: 48rem; } .admin p { margin: 6px 0 12px; }
.admin form { margin-top: 12px; } .admin form label { gap: 4px; }
.admin .enable { display: flex; align-items: center; width: 100%; }
.admin input[type="number"] { width: 8rem; }
.admin dl { display: grid; grid-template-columns: max-content auto; gap: 4px 18px; }
.admin dd { margin: 0; } .admin ul { padding-inline-start: 1.4rem; }
.admin .notice { border-inline-start: 4px solid #194b79; padding-inline-start: 10px; }
.toolbar { display: flex; justify-content: space-between; gap: 14px; flex-wrap: wrap; align-items: end; margin-bottom: 16px; }
form { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }
form label { display: grid; gap: 4px; font-weight: 600; } input, select, button { font: inherit; padding: 6px 8px; }
input[name="plate"] { width: 8.5em; }
input[type="datetime-local"] { font-size: .9rem; }
input, select { border: 1px solid #8190a0; background: white; border-radius: 5px; max-width: 100%; }
button { color: white; background: #194b79; border: 1px solid #194b79; border-radius: 5px; }
a { color: #134d80; text-underline-offset: 3px; } a:focus-visible, button:focus-visible { outline: 3px solid #ac5d00; }
fieldset { display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 4px 10px 8px; margin: 0; border: 1px solid #a2b2c2; border-radius: 5px; }
legend { color: #4b5d70; padding-inline: 4px; font-size: .875rem; }
fieldset label { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; font-size: .875rem; }
fieldset input { margin: 0; accent-color: #194b79; }
.scroll { overflow-x: auto; } table { width: 100%; border-collapse: collapse; background: white; }
caption { text-align: left; margin-bottom: 5px; }
th, td { text-align: left; padding: 7px clamp(6px, 1vw, 14px); border-bottom: 1px solid #d3dce5; white-space: nowrap; }
thead { background: #e3ebf4; } .registration { font-weight: 650; } section { margin-top: 26px; }
.photo { width: clamp(74px, 14vw, 212px); aspect-ratio: 3 / 2; display: block; object-fit: contain; border-radius: 4px; background: #e3ebf4; }
.no-photo { display: grid; place-items: center; font-size: .78rem; }
td[data-column] { white-space: normal; overflow-wrap: anywhere; }
figure { margin: 12px 0; } figure img { display: block; width: auto; max-width: min(100%, 640px); height: auto; border-radius: 5px; }
main:has(#show-make:not(:checked)) [data-column="make"],
main:has(#show-model:not(:checked)) [data-column="model"],
main:has(#show-colour:not(:checked)) [data-column="colour"] { display: none; }
"""
JS = """
for (const checkbox of document.querySelectorAll('[data-column-toggle]')) {
    const key = `platelog.column.${checkbox.dataset.columnToggle}`;
    try { checkbox.checked = localStorage.getItem(key) !== 'hidden'; } catch {}
    checkbox.addEventListener('change', () => {
        try { localStorage.setItem(key, checkbox.checked ? 'shown' : 'hidden'); } catch {}
    });
}

const form = document.querySelector('#register-search');
if (form) {
    const plate = form.elements.plate;
    const camera = form.elements.camera;
    const results = document.querySelector('#register-results');
    const status = document.querySelector('#search-status');
    let pending;

    async function filter() {
        pending?.abort();
        const request = new AbortController();
        pending = request;
        const params = new URLSearchParams();
        for (const [key, value] of new FormData(form)) {
            if (value.trim()) params.set(key, value.trim());
        }
        const query = params.size ? `?${params}` : '';
        status.textContent = '';
        try {
            const response = await fetch(`/register/results${query}`, { signal: request.signal });
            if (response.status === 400) throw new Error((await response.json()).detail);
            if (!response.ok) throw new Error('Could not update results. Try Search again.');
            const html = await response.text();
            if (request !== pending || request.signal.aborted) return;
            results.innerHTML = html;
            history.replaceState(history.state, '', `/${query}`);
        } catch (error) {
            if (request === pending && error.name !== 'AbortError') {
                status.textContent = error.message;
            }
        }
    }

    plate.addEventListener('input', filter);
    for (const input of [camera, form.elements.start, form.elements.end]) input.addEventListener('change', filter);
    form.addEventListener('submit', event => {
        if (event.submitter?.hasAttribute('data-export')) {
            const { start, end } = form.elements;
            if (!start.value || !end.value || start.value <= end.value) return;
            event.preventDefault();
            status.textContent = 'To must be the same minute as From or later.';
            end.focus();
            return;
        }
        event.preventDefault();
        filter();
    });
    form.querySelector('[data-clear-search]').addEventListener('click', event => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        form.reset();
        plate.value = '';
        camera.value = '';
        form.elements.start.value = '';
        form.elements.end.value = '';
        filter();
        plate.focus();
    });
}
"""
CAMERA_NAMES = {"demo-entry": "Demo entry", "demo-exit": "Demo exit"}
VEHICLE_COLUMNS = ("make", "model", "colour")
REPORT_LIMIT = 10_000
SESSION_SECONDS = 8 * 60 * 60
ADMIN_COOKIE = "platelog_admin"
LOG = logging.getLogger(__name__)
PHOTO_BUDGET = 32 * 1024 * 1024
OOXML = "http://schemas.openxmlformats.org/"
RELATIONSHIPS = OOXML + "package/2006/relationships"
OFFICE_REL = OOXML + "officeDocument/2006/relationships"
DRAWING = OOXML + "drawingml/2006/spreadsheetDrawing"
DRAWING_MAIN = OOXML + "drawingml/2006/main"


@dataclass(frozen=True)
class RegisterFilters:
    plate: str
    camera: str
    start: str
    end: str
    start_ms: int | None
    end_ms: int | None


def time_bound(text: str, zone: timezone | ZoneInfo, *, end: bool = False) -> int | None:
    if not text:
        return None
    try:
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d", text):
            raise ValueError
        local = datetime.fromisoformat(text)
        utc_times = {local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1)}
        valid = [value for value in utc_times if value.astimezone(zone).replace(tzinfo=None) == local]
        selected = max(valid) if end else min(valid)
        return int(selected.timestamp() * 1000) + (60_000 if end else 0)
    except (ValueError, OverflowError):
        raise HTTPException(400, f"Enter a valid {'To' if end else 'From'} date and time in {zone}.") from None


def worksheet(rows, image_rows: set[int] | None = None):
    content = []
    for number, row in enumerate(rows, 1):
        cells = []
        for index, value in enumerate(row):
            key = f"{chr(65 + index)}{number}"
            if type(value) is int:
                cells.append(f'<c r="{key}"><v>{value}</v></c>')
            else:
                # Inline strings stay literal even when camera metadata resembles a formula.
                safe = re.sub(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", str(value or ""))
                cells.append(f'<c r="{key}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(safe)}</t></is></c>')
        height = ' ht="78" customHeight="1"' if image_rows and number in image_rows else ""
        content.append(f'<row r="{number}"{height}>{"".join(cells)}</row>')
    columns = ('<col min="1" max="1" width="24" customWidth="1"/>'
               '<col min="2" max="11" width="18" customWidth="1"/>' if image_rows is not None else
               '<col min="1" max="2" width="22" customWidth="1"/>')
    drawing = '<drawing r:id="rId1"/>' if image_rows else ""
    return (f'<worksheet xmlns="{OOXML}spreadsheetml/2006/main" xmlns:r="{OFFICE_REL}">'
            f'<cols>{columns}</cols><sheetData>{"".join(content)}</sheetData>{drawing}</worksheet>')


def export_thumbnail(path: Path) -> tuple[bytes, int, int]:
    with Image.open(path, formats=["JPEG"]) as source:
        source.draft("RGB", (288, 192))
        thumbnail = source.convert("RGB")
        thumbnail.thumbnail((144, 96), Image.Resampling.LANCZOS)
    output = BytesIO()
    thumbnail.save(output, format="JPEG", quality=76, optimize=True)
    return output.getvalue(), thumbnail.width, thumbnail.height


def drawing_anchor(row: int, width: int, height: int) -> str:
    # Both corners lie inside the same cell so the picture moves and resizes with it.
    offset_x, offset_y = (156 - width) // 2, (104 - height) // 2

    def marker(name: str, x: int, y: int) -> str:
        return (f'<xdr:{name}><xdr:col>0</xdr:col><xdr:colOff>{x * 9525}</xdr:colOff>'
                f'<xdr:row>{row - 1}</xdr:row><xdr:rowOff>{y * 9525}</xdr:rowOff></xdr:{name}>')

    return (f'<xdr:twoCellAnchor editAs="twoCell">{marker("from", offset_x, offset_y)}'
            f'{marker("to", offset_x + width, offset_y + height)}<xdr:pic>'
            f'<xdr:nvPicPr><xdr:cNvPr id="{row}" name="Vehicle photo, row {row}"/>'
            '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr></xdr:nvPicPr>'
            f'<xdr:blipFill><a:blip r:embed="rId{row}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>'
            '<xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>'
            '</xdr:pic><xdr:clientData/></xdr:twoCellAnchor>')


class ReportTooLarge(ValueError):
    pass


def excel_report(rows: list[dict], filters: RegisterFilters, zone: timezone | ZoneInfo,
                 photo_path: Callable[[str], Path | None]) -> bytes:
    counts = Counter(row["camera"] for row in rows)
    summary = [("Filter", "Value"), ("From (inclusive)", filters.start or "Any time"),
               ("To (inclusive minute)", filters.end or "Any time"), ("Time zone", str(zone)),
               ("Registration", filters.plate or "All registrations"),
               ("Camera", CAMERA_NAMES.get(filters.camera, filters.camera) or "All cameras"),
               ("Passages", len(rows)), (), ("Camera", "Passages"),
               *((CAMERA_NAMES.get(camera, camera), count) for camera, count in sorted(counts.items()))]
    passages = [("Image", "Reg", "Date", "Time", "UTC offset", "Camera", "Direction", "Make", "Model", "Colour", "Photos")]
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        anchors = []
        image_links = []
        image_rows = set()
        image_bytes = 0
        for number, row in enumerate(rows, 2):
            capture = datetime.fromtimestamp(row["capture_ms"] / 1000, zone)
            image_label = "Image expired" if row.get("photo_expired") else "No image"
            if row["photo_id"]:
                path = photo_path(row["photo_id"])
                try:
                    photo = export_thumbnail(path) if path else None
                except (OSError, Image.DecompressionBombError):
                    photo = None
                image_label = "Image unavailable"
                if photo:
                    data, width, height = photo
                    image_bytes += len(data)
                    if image_bytes > PHOTO_BUDGET:
                        raise ReportTooLarge
                    archive.writestr(f"xl/media/vehicle-{number}.jpg", data)
                    anchors.append(drawing_anchor(number, width, height))
                    image_links.append(f'<Relationship Id="rId{number}" Type="{OFFICE_REL}/image" Target="../media/vehicle-{number}.jpg"/>')
                    image_rows.add(number)
                    image_label = ""
            passages.append((image_label, row["plate"], capture.strftime("%Y-%m-%d"), capture.strftime("%H:%M:%S"),
                             capture.strftime("%z"), CAMERA_NAMES.get(row["camera"], row["camera"]), row["direction"].title(),
                             row["make"], row["model"], row["colour"], row["images"]))

        types = ['<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
        if anchors:
            types.extend(('<Default Extension="jpg" ContentType="image/jpeg"/>',
                          '<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>'))
            archive.writestr("xl/worksheets/_rels/sheet2.xml.rels",
                             f'<Relationships xmlns="{RELATIONSHIPS}"><Relationship Id="rId1" Type="{OFFICE_REL}/drawing" Target="../drawings/drawing1.xml"/></Relationships>')
            archive.writestr("xl/drawings/_rels/drawing1.xml.rels", f'<Relationships xmlns="{RELATIONSHIPS}">{"".join(image_links)}</Relationships>')
            archive.writestr("xl/drawings/drawing1.xml", f'<xdr:wsDr xmlns:xdr="{DRAWING}" xmlns:a="{DRAWING_MAIN}" xmlns:r="{OFFICE_REL}">{"".join(anchors)}</xdr:wsDr>')
        entries = []
        links = []
        for index, (name, data) in enumerate((("Summary", summary), ("Passages", passages)), 1):
            path = f"worksheets/sheet{index}.xml"
            types.append(f'<Override PartName="/xl/{path}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            entries.append(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>')
            links.append(f'<Relationship Id="rId{index}" Type="{OFFICE_REL}/worksheet" Target="{path}"/>')
            archive.writestr(f"xl/{path}", worksheet(data, image_rows if index == 2 else None))
        archive.writestr("[Content_Types].xml", f'<Types xmlns="{OOXML}package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>{"".join(types)}</Types>')
        archive.writestr("_rels/.rels", f'<Relationships xmlns="{RELATIONSHIPS}"><Relationship Id="rId1" Type="{OFFICE_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{RELATIONSHIPS}">{"".join(links)}</Relationships>')
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{OOXML}spreadsheetml/2006/main" xmlns:r="{OFFICE_REL}"><sheets>{"".join(entries)}</sheets></workbook>')
    return output.getvalue()


def timestamp(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(sep=" ", timespec="milliseconds")


def page(title: str, content: str) -> str:
    tab = "admin" if title == "Admin" else "register"
    register_current = ' aria-current="page"' if tab == "register" else ""
    admin_current = ' aria-current="page"' if tab == "admin" else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(title)} | PlateLog</title><link rel="stylesheet" href="/preview.css"><script defer src="/preview.js"></script></head>
    <body><main><header><h1>PlateLog</h1><p>Simulated Axis camera events</p>
    <nav class="tabs" aria-label="PlateLog"><a href="/"{register_current}>Register</a>
    <a href="/admin"{admin_current}>Admin</a></nav></header>{content}</main></body></html>"""


def password_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**16, r=8, p=2, maxmem=96 * 1024**2)


def password_verifier(password: str) -> str:
    if not 14 <= len(password) or len(password.encode()) > 1024:
        raise ValueError("Use at least 14 characters and no more than 1024 bytes.")
    salt = secrets.token_bytes(16)
    key = password_key(password, salt)
    return f"{salt.hex()}:{key.hex()}"


def set_admin_password(store: Store, password: str) -> None:
    store.set_setting("admin_password", password_verifier(password), "Administrator password set")


def password_matches(password: str, verifier: str) -> bool:
    if not password or len(password.encode()) > 1024:
        return False
    salt, expected = verifier.split(":", 1)
    return hmac.compare_digest(password_key(password, bytes.fromhex(salt)), bytes.fromhex(expected))


async def admin_form(request: Request, limit: int = 4096) -> dict[str, str]:
    expected = f"{request.url.scheme}://{request.headers.get('host')}"
    if request.headers.get("origin") != expected:
        raise HTTPException(403, "Submit the form from the PlateLog Admin page.")
    if request.headers.get("content-type", "").partition(";")[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Expected a form submission.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "Admin form is too large.")
        body.extend(chunk)
    try:
        fields = parse_qs(body.decode("ascii"), keep_blank_values=True, max_num_fields=8, errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "Invalid form submission.") from exc
    if any(len(values) != 1 for values in fields.values()):
        raise HTTPException(400, "Duplicate form field.")
    return {name: values[0] for name, values in fields.items()}


def admin_routes(app: FastAPI, store: Store, persistent: bool, allow_http_admin: bool,
                 restart: Callable[[], None] | None, web_port: int | None,
                 change_web_port: Callable[[int], None] | None) -> None:
    sessions: dict[str, tuple[float, str, str]] = {}
    login_attempts: list[float] = []
    password_check = asyncio.Lock()
    restarting = False

    def transport_ok(request: Request) -> bool:
        return allow_http_admin or request.url.scheme == "https"

    def session_key(request: Request) -> str:
        token = request.cookies.get(ADMIN_COOKIE, "")
        return hashlib.sha256(token.encode()).hexdigest() if len(token) == 43 else ""

    def credential(request: Request) -> str | None:
        return store.setting("admin_password") if persistent and transport_ok(request) else None

    def authenticated(request: Request) -> str | None:
        state = sessions.get(session_key(request))
        if state and time.monotonic() < state[0] and state[2] == credential(request):
            return state[1]
        return None

    def show(content: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(page("Admin", f'<div class="admin"><h2>Admin</h2>{content}</div>'), status_code=status)

    def login_page(message: str = "", status: int = 200) -> HTMLResponse:
        notice = f'<p role="alert">{escape(message)}</p>' if message else ""
        return show(notice + '''<form method="post" action="/admin/login">
            <label>Administrator password <input type="password" name="password" autocomplete="current-password" required></label>
            <button type="submit">Sign in</button></form>''', status)

    def dashboard(csrf: str, message: str = "", status: int = 200) -> HTMLResponse:
        retention = store.retention()
        counts, audit = store.admin_overview()
        last = store.setting("retention_last_run")
        changes = "".join(f'<li>{escape(entry["activity"])} · {timestamp(entry["at_ms"])}</li>' for entry in audit)
        port_form = f'''<form method="post" action="/admin/web-port">
            <input type="hidden" name="csrf" value="{csrf}">
            <label>Web port <input type="number" name="port" min="1" max="65535" value="{web_port}" required></label>
            <p>Allow the new TCP port in your firewall before applying it.</p>
            <button type="submit">Save port and restart</button></form>''' if restart and change_web_port else ""
        restart_form = f'''<section><h3>Service</h3><form method="post" action="/admin/restart">
            <input type="hidden" name="csrf" value="{csrf}">
            <label class="enable"><input type="checkbox" name="confirm" value="yes" required> Confirm restart</label>
            <button type="submit">Restart PlateLog</button></form>{port_form}</section>''' if restart else ""
        return show(f'''<p class="notice" role="status">{escape(message)}</p><section><h3>Retention</h3>
            <p>Cleanup runs at startup and hourly. Enabling it can permanently remove expired records and photos.</p>
            <form method="post" action="/admin/settings"><input type="hidden" name="csrf" value="{csrf}">
            <label class="enable"><input type="checkbox" name="enabled" value="yes" {'checked' if retention['enabled'] else ''}>Enable automatic cleanup</label>
            <label>Keep passages (days) <input type="number" name="passage_days" min="1" max="3650" value="{retention['passage_days']}" required></label>
            <label>Keep photos (days) <input type="number" name="photo_days" min="1" max="3650" value="{retention['photo_days']}" required></label>
            <button type="submit">Save settings</button></form></section>
            <section><h3>Change administrator password</h3>
            <form method="post" action="/admin/password"><input type="hidden" name="csrf" value="{csrf}">
            <label>Current password <input type="password" name="current_password" autocomplete="current-password" required></label>
            <label>New password <input type="password" name="new_password" autocomplete="new-password" minlength="14" required></label>
            <label>Confirm new password <input type="password" name="confirm_password" autocomplete="new-password" minlength="14" required></label>
            <button type="submit">Change password</button></form></section>
            {restart_form}
            <section><h3>Storage</h3><dl><dt>Passages</dt><dd>{counts['passages']:,}</dd><dt>Photos</dt><dd>{counts['photos']:,}</dd>
            <dt>Photo storage</dt><dd>{counts['photo_bytes'] / 1_048_576:.2f} MiB</dd>
            <dt>Last cleanup (UTC)</dt><dd>{timestamp(int(last)) if last else 'None yet'}</dd></dl></section>
            <section><h3>Recent admin activity (UTC)</h3>{f'<ul>{changes}</ul>' if changes else '<p>No activity yet.</p>'}</section>
            <section><form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{csrf}">
            <button type="submit">Sign out</button></form></section>''', status)

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request, saved: bool = False, password_changed: bool = False):
        if not persistent:
            return show("<p>Admin needs a persistent data directory and a password set on the server.</p>")
        if not transport_ok(request):
            return show("<p>This server requires HTTPS for Admin sign-in.</p>")
        if not credential(request):
            return show("<p>Set an administrator password on the server to activate this tab.</p>")
        csrf = authenticated(request)
        if csrf:
            return dashboard(csrf, "Retention settings saved." if saved else "")
        return login_page("Password changed. Sign in with your new password." if password_changed else "")

    @app.post("/admin/login")
    async def login(request: Request):
        verifier = credential(request)
        if not verifier:
            raise HTTPException(403, "Admin sign-in is unavailable on this connection.")
        fields = await admin_form(request)
        now = time.monotonic()
        login_attempts[:] = [attempt for attempt in login_attempts if now - attempt < 300]
        if len(login_attempts) >= 5:
            return login_page("Too many attempts. Try again in five minutes.", 429)
        login_attempts.append(now)
        async with password_check:
            if (verifier != credential(request)
                    or not await run_in_threadpool(password_matches, fields.get("password", ""), verifier)):
                return login_page("Incorrect password.", 401)
            login_attempts.clear()
        for key in list(sessions):
            if sessions[key][0] <= now or sessions[key][2] != verifier:
                del sessions[key]
        sessions.pop(session_key(request), None)
        if len(sessions) >= 32:
            sessions.pop(next(iter(sessions)))
        token = secrets.token_urlsafe(32)
        sessions[hashlib.sha256(token.encode()).hexdigest()] = (now + SESSION_SECONDS, secrets.token_urlsafe(32), verifier)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(ADMIN_COOKIE, token, path="/admin", httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=SESSION_SECONDS)
        return response

    async def authorized_form(request: Request, limit: int = 4096) -> tuple[str, dict[str, str]]:
        csrf = authenticated(request)
        if not csrf:
            raise HTTPException(403, "Sign in as administrator.")
        fields = await admin_form(request, limit)
        if not hmac.compare_digest(csrf.encode(), fields.get("csrf", "").encode()):
            raise HTTPException(403, "Reload the Admin page before submitting.")
        return csrf, fields

    @app.post("/admin/settings")
    async def save_retention(request: Request):
        csrf, fields = await authorized_form(request)
        try:
            days = {name: int(fields[name]) for name in ("passage_days", "photo_days")}
            if not 1 <= days["photo_days"] <= days["passage_days"] <= 3650:
                raise ValueError
        except (KeyError, ValueError):
            return dashboard(csrf, "Enter 1–3650 days. Photo retention must be no longer than passage retention.", 400)
        enabled = fields.get("enabled") == "yes"
        settings = json.dumps({"enabled": enabled, **days})
        activity = f"Retention saved: {days['passage_days']} days for passages, {days['photo_days']} for photos, cleanup {'on' if enabled else 'off'}"
        await run_in_threadpool(store.set_setting, "retention", settings, activity)
        return RedirectResponse("/admin?saved=true", status_code=303)

    @app.post("/admin/logout")
    async def logout(request: Request):
        await authorized_form(request)
        sessions.pop(session_key(request), None)
        response = RedirectResponse("/admin", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/admin", secure=request.url.scheme == "https", httponly=True, samesite="strict")
        return response

    @app.post("/admin/password")
    async def change_password(request: Request):
        current = credential(request)
        csrf, fields = await authorized_form(request, limit=10_240)
        value = fields.get("new_password", "")
        if value != fields.get("confirm_password"):
            return dashboard(csrf, "The new passwords did not match.", 400)
        async with password_check:
            if not current or current != credential(request):
                raise HTTPException(403, "Sign in again to change the password.")
            if not await run_in_threadpool(password_matches, fields.get("current_password", ""), current):
                return dashboard(csrf, "Incorrect current password.", 401)
            try:
                replacement = await run_in_threadpool(password_verifier, value)
            except ValueError as exc:
                return dashboard(csrf, str(exc), 400)
            if not await run_in_threadpool(store.change_admin_password, current, replacement):
                raise HTTPException(403, "Sign in again to change the password.")
            sessions.clear()
            login_attempts.clear()
        response = RedirectResponse("/admin?password_changed=true", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/admin", secure=request.url.scheme == "https", httponly=True, samesite="strict")
        return response

    def restart_response(destination: str = "/admin"):
        response = show(f'<p>PlateLog is restarting. <a href="{escape(destination)}">Open Admin</a> in a moment to sign in again.</p>', 202)
        restart()
        return response

    @app.post("/admin/restart")
    async def restart_service(request: Request):
        nonlocal restarting
        csrf, fields = await authorized_form(request)
        if not restart or restarting:
            raise HTTPException(409, "PlateLog cannot accept a restart request right now.")
        if fields.get("confirm") != "yes":
            return dashboard(csrf, "Confirm the restart first.", 400)
        restarting = True
        try:
            await run_in_threadpool(store.record_admin_activity, "PlateLog restart requested")
        except Exception:
            restarting = False
            raise
        return restart_response()

    @app.post("/admin/web-port")
    async def save_web_port(request: Request):
        nonlocal restarting
        csrf, fields = await authorized_form(request)
        if not change_web_port or not restart or restarting:
            raise HTTPException(409, "The web port cannot be changed right now.")
        port_text = fields.get("port", "")
        if not re.fullmatch(r"[0-9]{1,5}", port_text) or not 1 <= int(port_text) <= 65535:
            return dashboard(csrf, "Enter a web port from 1 to 65535.", 400)
        port = int(port_text)
        if port == web_port:
            return dashboard(csrf, "PlateLog is already using this port.")
        destination = str(request.url.replace(port=port, path="/admin", query=""))
        restarting = True
        try:
            await run_in_threadpool(change_web_port, port)
        except OSError:
            restarting = False
            return dashboard(csrf, "PlateLog could not open that port. Choose another port.", 409)
        except Exception:
            restarting = False
            raise
        return restart_response(destination)


def make_preview(data_dir: Path, allowed_host: str, display_zone: timezone | ZoneInfo = timezone.utc,
                 *, persistent: bool = False, allow_http_admin: bool = True,
                 restart: Callable[[], None] | None = None, web_port: int | None = None,
                 change_web_port: Callable[[int], None] | None = None) -> FastAPI:
    if allowed_host != "*" and not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", allowed_host):
        raise ValueError("Expected a hostname, IPv4 address, or '*' for --allowed-host")
    store = Store(data_dir)

    async def supervise_retention():
        while True:
            try:
                await run_in_threadpool(store.apply_retention)
            except Exception:
                LOG.exception("PlateLog retention failed")
            await asyncio.sleep(3600)

    @asynccontextmanager
    async def lifespan(_app):
        cleanup = asyncio.create_task(supervise_retention()) if persistent else None
        try:
            yield
        finally:
            if cleanup:
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", allowed_host], www_redirect=False)
    admin_routes(app, store, persistent, allow_http_admin, restart, web_port, change_web_port)

    @app.middleware("http")
    async def preview_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'"
        return response

    @app.get("/healthz")
    def health():
        return {"status": "ready", "mode": "synthetic-preview"}

    @app.get("/preview.css")
    def stylesheet():
        return Response(CSS, media_type="text/css")

    @app.get("/preview.js")
    def browser_script():
        return Response(JS, media_type="text/javascript")

    def filters_for_request(plate: str = Query("", max_length=128), camera: str = Query("", max_length=128),
                            start: str = Query("", max_length=16), end: str = Query("", max_length=16)) -> RegisterFilters:
        lower = time_bound(start, display_zone)
        upper = time_bound(end, display_zone, end=True)
        if lower is not None and upper is not None and (start > end or lower >= upper):
            raise HTTPException(400, "To must be the same minute as From or later.")
        return RegisterFilters(plate.strip(), camera, start, end, lower, upper)

    def matching(filters: RegisterFilters, limit: int):
        return store.passages(filters.plate, filters.camera, limit, partial_plate=True,
                              start_ms=filters.start_ms, end_ms=filters.end_ms)

    @app.get("/", response_class=HTMLResponse)
    def register(filters: Annotated[RegisterFilters, Depends(filters_for_request)]):
        options = ['<option value="">All cameras</option>']
        for key in CAMERAS:
            selected = " selected" if filters.camera == key else ""
            options.append(f'<option value="{key}"{selected}>{CAMERA_NAMES[key]}</option>')
        controls = f'''<form id="register-search" action="/" method="get"><label>Plate <input type="search" name="plate" maxlength="128" value="{escape(filters.plate)}" autocomplete="off" spellcheck="false"></label>
            <label>Camera <select name="camera">{"".join(options)}</select></label>
            <label>From <input type="datetime-local" name="start" value="{escape(filters.start)}"></label>
            <label>To <input type="datetime-local" name="end" value="{escape(filters.end)}"></label>
            <button type="submit">Search</button><button type="submit" formaction="/register/export" data-export>Export Excel</button>
            <a href="/" data-clear-search>Clear</a></form>'''
        toggles = "".join(f'<label><input type="checkbox" id="show-{key}" data-column-toggle="{key}" checked> {key.title()}</label>'
                          for key in VEHICLE_COLUMNS)
        toolbar = f'<div class="toolbar">{controls}<fieldset><legend>Columns</legend>{toggles}</fieldset></div>'
        return page("Register", toolbar + '<p id="search-status" role="status"></p>' +
                    f'<div id="register-results">{render_results(filters)}</div>')

    def render_results(filters: RegisterFilters) -> str:
        rows = matching(filters, 101)
        caption = f"Passages · {escape(str(display_zone))}"
        if len(rows) > 100:
            caption += " · Latest 100 shown"
        result = []
        for row in rows[:100]:
            link = f"/passages/{row['id']}"
            plate_text = escape(row["plate"])
            photo_label = "Image expired" if row["photo_expired"] else "No image"
            photo = f'<span class="photo no-photo muted">{photo_label}</span>'
            if row["photo_id"]:
                photo = (f'<a href="{link}"><img class="photo" src="/images/{row["photo_id"]}" '
                         f'alt="Vehicle image for {plate_text}" loading="lazy" decoding="async"></a>')
            capture = datetime.fromtimestamp(row["capture_ms"] / 1000, display_zone)
            date = capture.strftime("%d/%m/%Y")
            time = capture.strftime("%H:%M:%S")
            cells = "".join(f'<td data-column="{key}">{escape(row[key]) if row[key] else "—"}</td>'
                            for key in VEHICLE_COLUMNS)
            result.append(f'''<tr><td>{photo}</td><td class="registration"><a href="{link}">{plate_text}</a></td>
                <td>{date}</td><td><time datetime="{capture.isoformat()}" title="{capture.strftime('%Z %z')}">{time}</time></td>{cells}</tr>''')
        if not result:
            return '<p role="status">No matching passages.</p>'
        headings = "".join(f'<th scope="col" data-column="{key}">{key.title()}</th>' for key in VEHICLE_COLUMNS)
        return f'''<div class="scroll"><table><caption>{caption}</caption>
            <thead><tr><th scope="col">Image</th><th scope="col">Reg</th><th scope="col">Date</th><th scope="col">Time</th>{headings}</tr></thead>
            <tbody>{"".join(result)}</tbody></table></div>'''

    @app.get("/register/results", response_class=HTMLResponse)
    def register_results(filters: Annotated[RegisterFilters, Depends(filters_for_request)]):
        return render_results(filters)

    @app.get("/register/export")
    def export_register(filters: Annotated[RegisterFilters, Depends(filters_for_request)]):
        rows = matching(filters, REPORT_LIMIT + 1)
        if len(rows) > REPORT_LIMIT:
            return HTMLResponse(page("Export", '<p>More than 10,000 passages match. Narrow the date range and try again.</p><p><a href="/">Back to register</a></p>'), status_code=400)
        try:
            report = excel_report(rows, filters, display_zone, store.image_path)
        except ReportTooLarge:
            return HTMLResponse(page("Export", '<p>The photos make this report too large. Narrow the date range and try again.</p><p><a href="/">Back to register</a></p>'), status_code=400)
        return Response(report,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="platelog-report.xlsx"'})

    @app.get("/passages/{passage_id}", response_class=HTMLResponse)
    def passage(passage_id: str):
        detail = store.detail(passage_id) if re.fullmatch(ID_PATTERN, passage_id) else None
        if detail is None:
            raise HTTPException(404, "Passage not found")
        plate = escape(detail["plate"])
        camera = escape(CAMERA_NAMES.get(detail["camera"], detail["camera"]))
        heading = f'<p><a href="/">Back to passages</a></p><section><h2>{plate}</h2><p class="muted">{camera} · {escape(detail["direction"].title())}</p></section>'
        history = []
        photos = []
        for packet in detail["history"]:
            state = escape(packet["state"].title())
            read = escape(packet["plate"])
            captured = timestamp(packet["capture_ms"])
            history.append(f'<tr><td>{state}</td><td>{read}</td><td>{escape(packet["direction"].title())}</td><td>{captured}</td></tr>')
            for image in packet["images"]:
                photos.append(f'''<figure><img src="/images/{image['id']}" alt="Synthetic vehicle photo">
                    <figcaption class="muted">{state} packet · {read} · {captured}</figcaption></figure>''')
        no_photo = "<p>Image expired.</p>" if detail["photo_expired"] else "<p>No photo received.</p>"
        return page(detail["plate"], heading + f'''<section><h2>Photos</h2>{"".join(photos) or no_photo}</section>
            <section><h2>Packet history</h2><div class="scroll"><table><thead><tr><th>Event</th><th>Plate read</th><th>Direction</th><th>Captured (UTC)</th></tr></thead>
            <tbody>{"".join(history)}</tbody></table></div></section>''')

    @app.get("/images/{image_id}")
    def image(image_id: str):
        path = store.image_path(image_id) if re.fullmatch(ID_PATTERN, image_id) else None
        if path is None:
            raise HTTPException(404, "Image not found")
        return FileResponse(path, media_type="image/jpeg")

    return app
