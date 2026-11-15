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
from sqlite3 import Row
from threading import Lock
from typing import Annotated, Callable
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from PIL import Image
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .credentials import (
    INITIAL_ADMIN_PREFIX,
    MIN_PASSWORD_LENGTH,
    initial_admin_verifier,
    password_matches,
    password_verifier,
)
from .daily_csv import export_due_report, report_folder, report_schedule
from .receiver import CAMERA_ID_PATTERN, CAMERAS, ID_PATTERN
from .store import Store

CSS = """
:root { font: 16px/1.5 system-ui, sans-serif; color: #172638; background: #f4f7fa; }
body { margin: 0; }
main { padding: 24px clamp(12px, 2vw, 32px) 40px; }
h1, h2, p { margin: 0; } h1 { font-size: 2.2rem; letter-spacing: -.045em; }
header { margin-bottom: 28px; } header p, .muted, caption { color: #4b5d70; }
.header-top { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px 24px; }
.register-clock { display: grid; gap: 2px; margin: 0; font-size: .875rem; }
.register-clock div { display: grid; grid-template-columns: 8.5em auto; gap: 8px; }
.register-clock dt { color: #4b5d70; }
.register-clock dd { margin: 0; font-variant-numeric: tabular-nums; }
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
.camera-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.camera-heading h3, .camera-editor h4 { margin: 0; }
.camera-add { padding: 5px 10px; border-radius: 5px; background: #194b79; color: white; text-decoration: none; }
.camera-table { border: 1px solid #d3dce5; border-radius: 6px; }
.camera-table th, .camera-table td { padding: 8px 10px; font-size: .875rem; }
.camera-table tbody th { font-weight: 650; }
.camera-table tbody tr:last-child > * { border-bottom: 0; }
.camera-table :is(th, td):last-child { position: sticky; inset-inline-end: 0; }
.camera-table td:last-child { background: white; }
.camera-table thead th:last-child { background: #e3ebf4; }
.camera-table tr[data-selected] > * { background: #edf4fa; }
.camera-scroll-hint { display: none; }
@media (max-width: 44rem) { .camera-scroll-hint { display: block; font-size: .875rem; } }
.camera-editor { padding: 12px 14px; margin-top: 12px; background: #fff; border: 1px solid #d3dce5; border-radius: 6px; }
.camera-editor code { overflow-wrap: anywhere; }
.admin .camera-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 13rem), 1fr)); }
.camera-form input, .camera-form select { box-sizing: border-box; width: 100%; min-width: 0; }
.camera-form .camera-auth-fields { display: contents; }
.camera-form:has(select option:checked:not([value="basic"])) .camera-auth-fields { display: none; }
.camera-password { display: grid; gap: 4px; }
.camera-password label { font-weight: 600; }
.camera-password [aria-invalid="true"] { border-color: #912323; }
.camera-password-error { color: #912323; font-weight: 600; }
.camera-form .camera-actions { display: flex; align-items: center; gap: 12px; grid-column: 1 / -1; }
#csv-reports .destination { flex: 1 1 18rem; max-width: 100%; min-width: 0; }
#csv-reports code { overflow-wrap: anywhere; }
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
.register-page-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 18px; margin-block: 8px; }
.register-pages { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.register-pages a { display: grid; min-width: 1.9rem; min-height: 2rem; place-items: center; padding-inline: 4px;
    border: 1px solid #d3dce5; border-radius: 4px; background: white; text-decoration: none; }
.register-pages a[aria-current="page"] { background: #194b79; border-color: #194b79; color: white; font-weight: 650; }
.register-live { font-size: .875rem; }
#register-summary { margin-bottom: 5px; }
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

const clock = document.querySelector('#register-clock');
if (clock) {
    const systemTime = clock.querySelector('[data-system-time]');
    const lastRead = clock.querySelector('[data-last-read]');
    const noReads = clock.querySelector('[data-no-reads]');
    let serverMs = Number(clock.dataset.serverMs);
    let syncedAt = performance.now();
    let zone = clock.dataset.zone;
    const dateOptions = { day: '2-digit', month: '2-digit', year: 'numeric',
                          hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' };
    let format = new Intl.DateTimeFormat('en-AU', { ...dateOptions, timeZone: zone });
    let syncing = false;

    function showTime(element, value) {
        const date = new Date(value);
        element.dateTime = date.toISOString();
        element.textContent = format.format(date);
    }

    function tick() {
        showTime(systemTime, serverMs + performance.now() - syncedAt);
    }

    async function syncClock() {
        if (syncing || document.hidden) return;
        syncing = true;
        try {
            const response = await fetch('/register/status');
            if (!response.ok) return;
            const status = await response.json();
            serverMs = status.server_ms;
            syncedAt = performance.now();
            if (zone !== status.zone) {
                zone = status.zone;
                format = new Intl.DateTimeFormat('en-AU', { ...dateOptions, timeZone: zone });
                clock.title = zone;
            }
            lastRead.hidden = status.last_read_ms === null;
            noReads.hidden = !lastRead.hidden;
            if (!lastRead.hidden) {
                showTime(lastRead, status.last_read_ms);
            }
            tick();
        } catch {} finally {
            syncing = false;
        }
    }

    tick();
    window.setInterval(tick, 1000);
    window.setInterval(syncClock, 5000);
    document.addEventListener('visibilitychange', syncClock);
}

const form = document.querySelector('#register-search');
if (form) {
    const plate = form.elements.plate;
    const camera = form.elements.camera;
    const results = document.querySelector('#register-results');
    const status = document.querySelector('#search-status');
    let pending;
    let rendered = results.innerHTML;
    let filtersApplied = true;

    function currentPage() {
        return Number(results.firstElementChild.dataset.registerPage);
    }

    function canRefresh() {
        return filtersApplied && currentPage() === 1 && !document.hidden;
    }

    async function update(page, { automatic = false, pager } = {}) {
        if (automatic && (pending || !canRefresh())) return;
        pending?.abort();
        const request = new AbortController();
        pending = request;
        const params = new URLSearchParams(automatic ? window.location.search : '');
        if (!automatic) {
            for (const [key, value] of new FormData(form)) {
                if (value.trim()) params.set(key, value.trim());
            }
        }
        if (page > 1) params.set('page', String(page));
        else params.delete('page');
        const query = params.size ? `?${params}` : '';
        if (!automatic) {
            filtersApplied = false;
            status.textContent = '';
        }
        try {
            const response = await fetch(`/register/results${query}`, { signal: request.signal });
            if (response.status === 401 && request === pending) {
                window.location.assign(`/login?${new URLSearchParams({ next: `/${query}` })}`);
                return;
            }
            if (response.status === 400) throw new Error((await response.json()).detail);
            if (!response.ok) throw new Error('Could not update results. Try Search again.');
            const html = await response.text();
            if (request !== pending || request.signal.aborted || (automatic && !canRefresh())) return;
            if (html !== rendered) {
                const scroll = results.querySelector('.scroll')?.scrollLeft;
                const active = document.activeElement;
                const focused = automatic && results.contains(active) && active.matches('a') ? active : null;
                const focusedPager = focused?.closest('[data-pager]')?.dataset.pager;
                results.innerHTML = html;
                rendered = html;
                const table = results.querySelector('.scroll');
                if (scroll && table) table.scrollLeft = scroll;
                if (focused) {
                    const region = focusedPager ? results.querySelector(`[data-pager="${focusedPager}"]`) : table;
                    Array.from(region?.querySelectorAll('a') ?? []).find(link =>
                        link.href === focused.href && link.textContent === focused.textContent)?.focus({ preventScroll: true });
                }
            }
            if (!automatic) {
                filtersApplied = true;
                const actualPage = currentPage();
                if (actualPage > 1) params.set('page', String(actualPage));
                else params.delete('page');
                history.replaceState(history.state, '', params.size ? `/?${params}` : '/');
                if (pager) {
                    results.querySelector('[data-pager="top"] [aria-current="page"]')?.focus({ preventScroll: true });
                    if (pager === 'bottom') results.scrollIntoView({ block: 'start' });
                }
            }
        } catch (error) {
            if (!automatic && request === pending && error.name !== 'AbortError') {
                status.textContent = error.message;
            }
        } finally {
            if (request === pending) pending = undefined;
        }
    }

    const filter = () => update(1);
    const refresh = () => update(1, { automatic: true });
    window.setInterval(refresh, 5000);
    document.addEventListener('visibilitychange', refresh);
    results.addEventListener('click', event => {
        const link = event.target.closest('a[data-page]');
        if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        update(Number(link.dataset.page), { pager: link.closest('[data-pager]')?.dataset.pager ?? 'top' });
    });

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
CAMERA_AUTH_LABELS = {"disabled": "Disabled", "basic": "HTTP Basic", "none": "No credentials"}
VEHICLE_COLUMNS = ("make", "model", "colour")
REPORT_LIMIT = 10_000
PHOTO_LIMIT = 5_000
REGISTER_PAGE_SIZE = 50
REGISTER_PAGE_LINKS = 10
SESSION_SECONDS = 8 * 60 * 60
ADMIN_COOKIE = "platelog_admin"
STAFF_COOKIE = "platelog_staff"
LOG = logging.getLogger(__name__)
PHOTO_BUDGET = 32 * 1024 * 1024
_excel_export_lock = Lock()
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
    zone: timezone | ZoneInfo


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
                 photo_path: Callable[[str], Path | None], *, include_photos: bool = True) -> bytes:
    if include_photos and sum(bool(row["photo_id"]) for row in rows) > PHOTO_LIMIT:
        raise ReportTooLarge(f"The selection has photos for more than {PHOTO_LIMIT:,} passages.")
    counts = Counter(row["camera"] for row in rows)
    summary = [("Filter", "Value"), ("From (inclusive)", filters.start or "Any time"),
               ("To (inclusive minute)", filters.end or "Any time"), ("Time zone", str(zone)),
               ("Registration", filters.plate or "All registrations"),
               ("Camera", CAMERA_NAMES.get(filters.camera, filters.camera) or "All cameras"),
               ("Passages", len(rows)), ("Embedded photos", "Included" if include_photos else "Omitted"),
               (), ("Camera", "Passages"),
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
            if not include_photos:
                image_label = ""
            elif row["photo_id"]:
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
                        raise ReportTooLarge(f"The combined thumbnails exceed {PHOTO_BUDGET // (1024 * 1024)} MiB.")
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


def page(title: str, content: str, *, show_admin: bool = True, header_extra: str = "") -> str:
    tab = "admin" if title == "Admin" else "register"
    register_current = ' aria-current="page"' if tab == "register" else ""
    admin_current = ' aria-current="page"' if tab == "admin" else ""
    admin_link = f'<a href="/admin"{admin_current}>Admin</a>' if show_admin else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(title)} | PlateLog</title><link rel="stylesheet" href="/preview.css"><script defer src="/preview.js"></script></head>
    <body><main><header><div class="header-top"><div><h1>PlateLog</h1><p>Vehicle passage register</p></div>{header_extra}</div>
    <nav class="tabs" aria-label="PlateLog"><a href="/"{register_current}>Register</a>
    {admin_link}</nav></header>{content}</main></body></html>"""


def register_page(request: Request, title: str, content: str, *, header_extra: str = "") -> str:
    return page(title, content, show_admin=not getattr(request.state, "staff_register_csrf", None),
                header_extra=header_extra)


def set_admin_password(store: Store, password: str) -> None:
    store.set_setting("admin_password", password_verifier(password), "Administrator password set")


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


@dataclass(frozen=True)
class CameraFormError:
    camera_id: str
    mode: str
    username: str
    message: str
    password_invalid: bool = False


def camera_table(rows: list[Row], selected: str | None) -> str:
    if not rows:
        return '<p class="muted">No cameras have been added.</p>'
    entries = []
    for row in rows:
        camera_id = escape(row["id"])
        camera_url = urlencode({"camera": row["id"]})
        mode = row["mode"]
        username = escape(row["username"]) if mode == "basic" and row["username"] else "—"
        active = " data-selected" if row["id"] == selected else ""
        entries.append(f'''<tr{active}><th scope="row">{camera_id}</th>
            <td><code>/api/axis/{camera_id}/events</code></td><td>{CAMERA_AUTH_LABELS[mode]}</td>
            <td>{username}</td><td><a href="/admin?{camera_url}#camera-editor" aria-label="Edit {camera_id}">Edit</a></td></tr>''')
    return f'''<p class="camera-scroll-hint muted">Scroll sideways to see all columns.</p>
        <div class="scroll camera-table" role="region" aria-label="Saved Axis cameras" tabindex="0">
        <table aria-label="Axis cameras"><thead><tr><th scope="col">Camera</th><th scope="col">POST path</th>
        <th scope="col">Authentication</th><th scope="col">Username</th><th scope="col">Actions</th></tr></thead>
        <tbody>{"".join(entries)}</tbody></table></div>'''


def camera_form(csrf: str, camera_id: str = "", mode: str = "disabled", username: str = "", *,
                error: CameraFormError | None = None) -> str:
    if error:
        mode, username = error.mode, error.username
    options = "".join(f'<option value="{value}"{" selected" if value == mode else ""}>{label}</option>'
                      for value, label in CAMERA_AUTH_LABELS.items())
    safe_id = escape(camera_id)
    draft_id = escape(error.camera_id) if error else ""
    identity = (f'<input type="hidden" name="camera_id" value="{safe_id}">' if camera_id else
                f'<label>Camera ID <input name="camera_id" value="{draft_id}" maxlength="64" placeholder="entry" required></label>')
    password_id = f"camera-password-saved-{safe_id}" if camera_id else "camera-password-new"
    hint_id = f"{password_id}-hint"
    password_invalid = error is not None and error.password_invalid
    if password_invalid:
        hint = f'<small id="{hint_id}" class="camera-password-error" role="alert">{escape(error.message)}</small>'
    else:
        keep = " Leave blank to keep it for the same username." if camera_id and username else ""
        hint = f'<small id="{hint_id}" class="muted">At least {MIN_PASSWORD_LENGTH} characters.{keep}</small>'
    invalid = ' aria-invalid="true" autofocus' if password_invalid else ""
    alert = f'<p class="camera-password-error" role="alert">{escape(error.message)}</p>' if error and not password_invalid else ""
    title = f"Edit {safe_id}" if camera_id else "Add a camera"
    path = f'<p>POST path <code>/api/axis/{safe_id}/events</code></p>' if camera_id else ""
    return f'''<div id="camera-editor" class="camera-editor"><h4>{title}</h4>{path}{alert}
    <form class="camera-form" method="post" action="/admin/cameras#camera-editor"><input type="hidden" name="csrf" value="{csrf}">
        {identity}<label>POST authentication <select name="mode">{options}</select></label>
        <div class="camera-auth-fields">
            <label>Username <input name="username" maxlength="128" autocomplete="off" value="{escape(username)}"></label>
            <div class="camera-password"><label for="{password_id}">Password</label>
            <input id="{password_id}" type="password" name="password" autocomplete="new-password"
                minlength="{MIN_PASSWORD_LENGTH}" aria-describedby="{hint_id}"{invalid}>
            {hint}</div>
        </div>
        <div class="camera-actions"><button type="submit">{'Save camera' if camera_id else 'Add camera'}</button>
        <a href="/admin#axis-cameras">Cancel</a></div></form></div>'''


def csv_schedule_form(store: Store, csrf: str, zone: str) -> str:
    schedule = report_schedule(store)
    stored_status = store.setting("daily_csv_status")
    last = json.loads(stored_status) if stored_status else None
    result = "<p>No export has run yet.</p>"
    if last:
        label = "Last export failed" if last["result"] == "error" else "Last successful export"
        result = (f'<p>{label} on {escape(last["attempted_at"])}. {escape(last["message"])}</p>'
                  f'<p><code>{escape(last["file"])}</code></p>')
    return f'''<section id="csv-reports"><h3>Daily CSV report</h3>
        <p>Exports the previous calendar day in {escape(zone)}, with a header row and one row per passage.</p>
        <form method="post" action="/admin/csv-schedule"><input type="hidden" name="csrf" value="{csrf}">
        <label class="enable"><input type="checkbox" name="enabled" value="yes" {'checked' if schedule['enabled'] else ''}> Enable daily CSV export</label>
        <label>Run time <input type="time" name="run_time" value="{escape(schedule['run_time'])}" required></label>
        <label class="destination">Destination folder <input name="folder" value="{escape(schedule['folder'])}" maxlength="1024" spellcheck="false"></label>
        <p>Use a folder on the server or a Windows UNC share that the PlateLog service account can write to.
        PlateLog names each file <code>platelog-YYYY-MM-DD.csv</code>.</p>
        <button type="submit">Save CSV schedule</button></form>{result}</section>'''


def register_destination(value: str) -> str:
    if len(value) > 2048 or not value.startswith("/") or "\\" in value or any(ord(char) < 32 for char in value):
        return "/"
    try:
        url = urlsplit(value)
    except ValueError:
        return "/"
    if url.netloc or url.scheme or url.fragment or value.startswith("//"):
        return "/"
    if url.path == "/" or re.fullmatch(r"/passages/[a-f0-9]{32}", url.path):
        return value
    return "/"


def admin_routes(app: FastAPI, store: Store, persistent: bool, allow_http_admin: bool,
                 restart: Callable[[], None] | None, web_port: int | None,
                 change_web_port: Callable[[int], None] | None, display_zone: timezone | ZoneInfo,
                 camera_port: int | None, camera_online: bool, change_camera_port: Callable[[int], None] | None):
    sessions: dict[str, tuple[float, str, str]] = {}
    staff_sessions: dict[str, tuple[float, str, str]] = {}
    login_attempts: list[float] = []
    staff_attempts: list[float] = []
    password_check = asyncio.Lock()
    restarting = False

    def transport_ok(request: Request) -> bool:
        return allow_http_admin or request.url.scheme == "https"

    def session_key(request: Request, cookie: str = ADMIN_COOKIE) -> str:
        token = request.cookies.get(cookie, "")
        return hashlib.sha256(token.encode()).hexdigest() if len(token) == 43 else ""

    def credential(request: Request) -> str | None:
        return store.setting("admin_password") if persistent and transport_ok(request) else None

    def session_csrf(state: tuple[float, str, str] | None, revision: str | None) -> str | None:
        if state and time.monotonic() < state[0] and state[2] == revision:
            return state[1]
        return None

    def admin_csrf(request: Request, *, allow_initial: bool = False) -> str | None:
        state = sessions.get(session_key(request))
        revision = credential(request) if state else None
        if not revision or (not allow_initial and revision.startswith(INITIAL_ADMIN_PREFIX)):
            return None
        return session_csrf(state, revision)

    def staff_csrf(request: Request, config: dict) -> str | None:
        if config["required"] and transport_ok(request):
            return session_csrf(staff_sessions.get(session_key(request, STAFF_COOKIE)), config["revision"])
        return None

    def require_register(request: Request) -> None:
        if not persistent:
            return
        config = store.register_access()
        if not config["required"] or admin_csrf(request):
            return
        if csrf := staff_csrf(request, config):
            request.state.staff_register_csrf = csrf
            return
        if request.url.path == "/" or request.url.path.startswith("/passages/"):
            destination = request.url.path
            if request.url.query:
                destination += "?" + request.url.query
            raise HTTPException(303, headers={"Location": "/login?" + urlencode({"next": register_destination(destination)})})
        raise HTTPException(401, "Sign in to view the register.")

    def sign_in(request: Request, destination: str, cookie: str, active: dict, revision: str) -> RedirectResponse:
        now = time.monotonic()
        for key in list(active):
            if active[key][0] <= now or active[key][2] != revision:
                del active[key]
        active.pop(session_key(request, cookie), None)
        if len(active) >= 32:
            active.pop(next(iter(active)))
        token = secrets.token_urlsafe(32)
        active[hashlib.sha256(token.encode()).hexdigest()] = (now + SESSION_SECONDS, secrets.token_urlsafe(32), revision)
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(cookie, token, path="/", httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=SESSION_SECONDS)
        if cookie == ADMIN_COOKIE:
            response.delete_cookie(cookie, path="/admin", secure=request.url.scheme == "https", httponly=True, samesite="strict")
        return response

    def sign_out(request: Request, destination: str, cookie: str, active: dict) -> RedirectResponse:
        active.pop(session_key(request, cookie), None)
        response = RedirectResponse(destination, status_code=303)
        response.delete_cookie(cookie, path="/", secure=request.url.scheme == "https", httponly=True, samesite="strict")
        return response

    def register_login_page(destination: str, message: str = "", status: int = 200) -> HTMLResponse:
        notice = f'<p role="alert">{escape(message)}</p>' if message else ""
        return HTMLResponse(page("Staff sign-in", f'''<section><h2>Staff sign-in</h2>{notice}
            <form method="post" action="/login"><input type="hidden" name="next" value="{escape(destination)}">
            <label>Staff password <input type="password" name="password" autocomplete="current-password" required></label>
            <button type="submit">Sign in</button></form></section>'''), status_code=status)

    @app.get("/login")
    def register_login(request: Request, next: str = "/"):
        destination = register_destination(next)
        config = store.register_access() if persistent else None
        if not config or not config["required"] or admin_csrf(request) or staff_csrf(request, config):
            return RedirectResponse(destination, status_code=303)
        if not transport_ok(request):
            return HTMLResponse(page("Staff sign-in", "<p>This server requires HTTPS for staff sign-in.</p>"), status_code=403)
        return register_login_page(destination)

    @app.post("/login")
    async def register_sign_in(request: Request):
        fields = await admin_form(request, limit=12_288)
        destination = register_destination(fields.get("next", ""))
        config = store.register_access() if persistent else None
        if not config or not config["required"]:
            return RedirectResponse(destination, status_code=303)
        if not transport_ok(request):
            raise HTTPException(403, "Staff sign-in requires HTTPS.")
        now = time.monotonic()
        staff_attempts[:] = [attempt for attempt in staff_attempts if now - attempt < 300]
        if len(staff_attempts) >= 5:
            return register_login_page(destination, "Too many attempts. Try again in five minutes.", 429)
        staff_attempts.append(now)
        async with password_check:
            if (not await run_in_threadpool(password_matches, fields.get("password", ""), config["verifier"])
                    or config["revision"] != store.register_access()["revision"]):
                return register_login_page(destination, "Incorrect staff password.", 401)
            staff_attempts.clear()
        return sign_in(request, destination, STAFF_COOKIE, staff_sessions, config["revision"])

    @app.post("/logout")
    async def register_logout(request: Request):
        fields = await admin_form(request)
        config = store.register_access() if persistent else None
        csrf = staff_csrf(request, config) if config else None
        if not csrf or not hmac.compare_digest(csrf.encode(), fields.get("csrf", "").encode()):
            raise HTTPException(403, "Reload the register before signing out.")
        return sign_out(request, "/", STAFF_COOKIE, staff_sessions)

    def show(content: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(page("Admin", f'<div class="admin"><h2>Admin</h2>{content}</div>'), status_code=status)

    def login_page(message: str = "", status: int = 200) -> HTMLResponse:
        notice = f'<p role="alert">{escape(message)}</p>' if message else ""
        return show(notice + '''<form method="post" action="/admin/login">
            <label>Administrator password <input type="password" name="password" autocomplete="current-password" required></label>
            <button type="submit">Sign in</button></form>''', status)

    def password_form(csrf: str) -> str:
        return f'''<section><h3>Change administrator password</h3>
            <form method="post" action="/admin/password"><input type="hidden" name="csrf" value="{csrf}">
            <label>Current password <input type="password" name="current_password" autocomplete="current-password" required></label>
            <label>New password <input type="password" name="new_password" autocomplete="new-password" minlength="14" required></label>
            <label>Confirm new password <input type="password" name="confirm_password" autocomplete="new-password" minlength="14" required></label>
            <button type="submit">Change password</button></form></section>'''

    def initial_password_page(csrf: str, message: str = "Choose a new password to unlock Admin.", status: int = 200) -> HTMLResponse:
        return show(f'<p role="status">{escape(message)}</p>{password_form(csrf)}', status)

    def dashboard(csrf: str, message: str = "", status: int = 200, *, camera_selection: str | None = None,
                  camera_error: CameraFormError | None = None, camera_saved: str = "") -> HTMLResponse:
        retention = store.retention()
        counts, audit = store.admin_overview()
        last = store.setting("retention_last_run")
        changes = "".join(f'<li>{escape(entry["activity"])} · {timestamp(entry["at_ms"])}</li>' for entry in audit)
        camera_rows = store.cameras()
        if camera_error:
            camera_selection = camera_error.camera_id
        selected = next((row for row in camera_rows if row["id"] == camera_selection), None)
        editor = ""
        if selected is not None:
            editor = camera_form(csrf, selected["id"], selected["mode"], selected["username"], error=camera_error)
        elif camera_selection == "" or camera_error:
            editor = camera_form(csrf, error=camera_error)
        camera_notice = f'<p role="status">Saved camera {escape(camera_saved)}.</p>' if camera_saved else ""
        zone = store.setting("display_timezone") or str(display_zone)
        port_form = f'''<form method="post" action="/admin/web-port">
            <input type="hidden" name="csrf" value="{csrf}">
            <label>Staff web port <input type="number" name="port" min="1" max="65535" value="{web_port}" required></label>
            <p>Allow the new TCP port in your firewall before applying it.</p>
            <button type="submit">Save port and restart</button></form>''' if restart and change_web_port else ""
        restart_form = f'''<section><h3>Service</h3><form method="post" action="/admin/restart">
            <input type="hidden" name="csrf" value="{csrf}">
            <label class="enable"><input type="checkbox" name="confirm" value="yes" required> Confirm restart</label>
            <button type="submit">Restart PlateLog</button></form>{port_form}</section>''' if restart else ""
        camera_port_form = f'''<form method="post" action="/admin/camera-port">
            <input type="hidden" name="csrf" value="{csrf}">
            <label>Camera POST port <input type="number" name="port" min="1" max="65535" value="{camera_port or ''}" required></label>
            <p>Allow this TCP port for the cameras in your firewall before applying a change.</p>
            <button type="submit">Save camera port and restart</button></form>''' if restart and change_camera_port else ""
        camera_port_status = (f"PlateLog listens for camera events on port {camera_port}." if camera_online else
                              f"The camera listener is offline on port {camera_port}.")
        camera_help = ("Use this port with the POST path shown for each camera." if change_camera_port else
                       "In Docker, send events to the host's published camera port and the POST path shown for each camera.")
        access = store.register_access()
        password_note = "Leave blank to keep the current staff password." if access["verifier"] else "Use at least 14 characters."
        access_form = f'''<section id="register-access"><h3>Register access</h3>
            <form method="post" action="/admin/register-access"><input type="hidden" name="csrf" value="{csrf}">
            <label class="enable"><input type="checkbox" name="required" value="yes" {'checked' if access['required'] else ''}> Require staff sign-in to view the register</label>
            <label>New staff password <input type="password" name="password" minlength="14" autocomplete="new-password"></label>
            <button type="submit">Save register access</button></form>
            <p>{password_note} The staff password opens the register. The administrator password opens both the register and Admin.</p></section>'''
        return show(f'''<p class="notice" role="status">{escape(message)}</p>{access_form}{restart_form}
            <section><h3>Display</h3><form method="post" action="/admin/timezone">
            <input type="hidden" name="csrf" value="{csrf}">
            <label>Time zone <input name="timezone" maxlength="128" value="{escape(zone)}" required></label>
            <p>Use an IANA time zone such as Australia/Sydney.</p><button type="submit">Save time zone</button></form></section>
            <section id="axis-cameras"><div class="camera-heading"><h3>Axis cameras</h3>
            <a class="camera-add" href="/admin?camera=#camera-editor">Add camera</a></div>
            <p>{camera_port_status} {camera_help}</p>{camera_port_form}{camera_notice}
            {camera_table(camera_rows, camera_selection)}{editor}</section>
            <section><h3>Retention</h3>
            <p>Cleanup runs at startup and hourly. Enabling it can permanently remove expired records and photos.</p>
            <form method="post" action="/admin/settings"><input type="hidden" name="csrf" value="{csrf}">
            <label class="enable"><input type="checkbox" name="enabled" value="yes" {'checked' if retention['enabled'] else ''}>Enable automatic cleanup</label>
            <label>Keep passages (days) <input type="number" name="passage_days" min="1" max="3650" value="{retention['passage_days']}" required></label>
            <label>Keep photos (days) <input type="number" name="photo_days" min="1" max="3650" value="{retention['photo_days']}" required></label>
            <button type="submit">Save settings</button></form>
            <form method="post" action="/admin/purge"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="passage_days" value="{retention['passage_days']}">
            <input type="hidden" name="photo_days" value="{retention['photo_days']}">
            <label class="enable"><input type="checkbox" name="confirm" value="yes" required>
            Permanently delete passages older than {retention['passage_days']} days and photos older than {retention['photo_days']} days</label>
            <button type="submit">Purge now</button></form></section>
            {csv_schedule_form(store, csrf, zone)}{password_form(csrf)}
            <section><h3>Storage</h3><dl><dt>Passages</dt><dd>{counts['passages']:,}</dd><dt>Photos</dt><dd>{counts['photos']:,}</dd>
            <dt>Events</dt><dd>{counts['packets']:,}</dd>
            <dt>Photo storage</dt><dd>{counts['photo_bytes'] / 1_048_576:.2f} MiB</dd>
            <dt>Last cleanup (UTC)</dt><dd>{timestamp(int(last)) if last else 'None yet'}</dd></dl></section>
            <section><h3>Recent admin activity (UTC)</h3>{f'<ul>{changes}</ul>' if changes else '<p>No activity yet.</p>'}</section>
            <section><form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{csrf}">
            <button type="submit">Sign out</button></form></section>''', status)

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request, saved: bool = False, password_changed: bool = False, csv_saved: bool = False,
              access_saved: bool = False, camera: str | None = None, camera_saved: str = ""):
        if not persistent:
            return show("<p>Admin needs a persistent data directory and a password set on the server.</p>")
        if not transport_ok(request):
            return show("<p>This server requires HTTPS for Admin sign-in.</p>")
        current = credential(request)
        if not current:
            return show("<p>Set an administrator password on the server to activate this tab.</p>")
        csrf = admin_csrf(request, allow_initial=True)
        if csrf:
            if current.startswith(INITIAL_ADMIN_PREFIX):
                return initial_password_page(csrf)
            message = ("Register access saved." if access_saved else "CSV schedule saved." if csv_saved else
                       "Retention settings saved." if saved else "")
            return dashboard(csrf, message, camera_selection=camera, camera_saved=camera_saved)
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
                    or not await run_in_threadpool(password_matches, fields.get("password", ""),
                                                   verifier.removeprefix(INITIAL_ADMIN_PREFIX))):
                return login_page("Incorrect password.", 401)
            login_attempts.clear()
        return sign_in(request, "/admin", ADMIN_COOKIE, sessions, verifier)

    async def authorized_form(request: Request, limit: int = 4096, *, allow_initial: bool = False) -> tuple[str, dict[str, str]]:
        csrf = admin_csrf(request, allow_initial=allow_initial)
        if not csrf:
            raise HTTPException(403, "Sign in as administrator.")
        fields = await admin_form(request, limit)
        if not hmac.compare_digest(csrf.encode(), fields.get("csrf", "").encode()):
            raise HTTPException(403, "Reload the Admin page before submitting.")
        return csrf, fields

    @app.post("/admin/register-access")
    async def save_register_access(request: Request):
        csrf, fields = await authorized_form(request)
        required = fields.get("required") == "yes"
        password = fields.get("password", "")
        try:
            verifier = await run_in_threadpool(password_verifier, password) if password else ""
            await run_in_threadpool(store.set_register_access, required, verifier)
        except ValueError as exc:
            return dashboard(csrf, str(exc), 400)
        return RedirectResponse("/admin?access_saved=true#register-access", status_code=303)

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

    @app.post("/admin/csv-schedule")
    async def save_csv_schedule(request: Request):
        csrf, fields = await authorized_form(request, limit=16_384)
        run_time = fields.get("run_time", "")
        folder = fields.get("folder", "").strip()
        enabled = fields.get("enabled") == "yes"
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", run_time):
            return dashboard(csrf, "Enter a run time between 00:00 and 23:59.", 400)
        if len(folder) > 1024:
            return dashboard(csrf, "Use a destination folder of at most 1024 characters.", 400)
        if enabled or folder:
            try:
                folder = str(report_folder(folder))
            except ValueError as exc:
                return dashboard(csrf, str(exc), 400)
        schedule = json.dumps({"enabled": enabled, "run_time": run_time, "folder": folder})
        activity = "Daily CSV schedule enabled" if enabled else "Daily CSV schedule disabled"
        await run_in_threadpool(store.set_setting, "daily_csv_schedule", schedule, activity)
        return RedirectResponse("/admin?csv_saved=true#csv-reports", status_code=303)

    @app.post("/admin/purge")
    async def purge(request: Request):
        csrf, fields = await authorized_form(request)
        if fields.get("confirm") != "yes":
            return dashboard(csrf, "Confirm permanent deletion first.", 400)
        try:
            limits = int(fields.get("passage_days", "")), int(fields.get("photo_days", ""))
        except ValueError:
            return dashboard(csrf, "Review the saved age limits first.", 400)
        try:
            passages, photos = await run_in_threadpool(store.apply_retention, confirmed_limits=limits)
        except ValueError as exc:
            return dashboard(csrf, str(exc), 409)
        return dashboard(csrf, f"Purged {passages} passages and {photos} photos.")

    @app.post("/admin/cameras")
    async def save_camera(request: Request):
        csrf, fields = await authorized_form(request)
        camera_id = fields.get("camera_id", "").strip()
        mode = fields.get("mode", "")
        username = fields.get("username", "").strip() if mode == "basic" else ""
        password = fields.get("password", "") if mode == "basic" else ""

        def reject(message: str, *, password_invalid: bool = False) -> HTMLResponse:
            error = CameraFormError(camera_id, mode, username, message, password_invalid)
            return dashboard(csrf, status=400, camera_error=error)

        if not re.fullmatch(CAMERA_ID_PATTERN, camera_id) or camera_id in CAMERAS:
            return reject("Use a unique camera ID with lowercase letters, digits, dashes or underscores.")
        if mode not in CAMERA_AUTH_LABELS:
            return reject("Choose a camera authentication mode.")
        if mode == "basic" and not re.fullmatch(r"[!-9;-~]{1,128}", username):
            return reject("Enter a camera username without spaces or colons.")
        try:
            async with password_check:
                verifier = await run_in_threadpool(password_verifier, password) if password else ""
            await run_in_threadpool(store.save_camera, camera_id, mode, username, verifier)
        except ValueError as exc:
            return reject(str(exc), password_invalid=True)
        return RedirectResponse(f'/admin?{urlencode({"camera_saved": camera_id})}#axis-cameras', status_code=303)

    @app.post("/admin/timezone")
    async def save_timezone(request: Request):
        csrf, fields = await authorized_form(request)
        name = fields.get("timezone", "").strip()
        try:
            if len(name) > 128:
                raise ValueError
            ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            return dashboard(csrf, "Enter a valid IANA time zone, such as Australia/Sydney.", 400)
        await run_in_threadpool(store.set_setting, "display_timezone", name, f"Display time zone changed to {name}")
        return dashboard(csrf, "Time zone saved.")

    @app.post("/admin/logout")
    async def logout(request: Request):
        await authorized_form(request)
        staff_sessions.pop(session_key(request, STAFF_COOKIE), None)
        response = sign_out(request, "/admin", ADMIN_COOKIE, sessions)
        response.delete_cookie(STAFF_COOKIE, path="/", secure=request.url.scheme == "https", httponly=True, samesite="strict")
        return response

    @app.post("/admin/password")
    async def change_password(request: Request):
        current = credential(request)
        csrf, fields = await authorized_form(request, limit=10_240, allow_initial=True)
        if not current or current != credential(request):
            raise HTTPException(403, "Sign in again to change the password.")
        render = initial_password_page if current.startswith(INITIAL_ADMIN_PREFIX) else dashboard
        value = fields.get("new_password", "")
        if value != fields.get("confirm_password"):
            return render(csrf, "The new passwords did not match.", 400)
        async with password_check:
            if not current or current != credential(request):
                raise HTTPException(403, "Sign in again to change the password.")
            if not await run_in_threadpool(password_matches, fields.get("current_password", ""),
                                           current.removeprefix(INITIAL_ADMIN_PREFIX)):
                return render(csrf, "Incorrect current password.", 401)
            try:
                replacement = await run_in_threadpool(password_verifier, value)
            except ValueError as exc:
                return render(csrf, str(exc), 400)
            if not await run_in_threadpool(store.change_admin_password, current, replacement):
                raise HTTPException(403, "Sign in again to change the password.")
            sessions.clear()
            login_attempts.clear()
        return sign_out(request, "/admin?password_changed=true", ADMIN_COOKIE, sessions)

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

    async def save_port(request: Request, name: str, current_port: int | None, other_port: int | None,
                        change_port: Callable[[int], None] | None, move_admin: bool):
        nonlocal restarting
        csrf, fields = await authorized_form(request)
        if not change_port or not restart or restarting:
            raise HTTPException(409, f"The {name} port cannot be changed right now.")
        port_text = fields.get("port", "")
        if not re.fullmatch(r"[0-9]{1,5}", port_text) or not 1 <= int(port_text) <= 65535:
            return dashboard(csrf, "Enter a TCP port from 1 to 65535.", 400)
        port = int(port_text)
        if port == current_port:
            return dashboard(csrf, "PlateLog is already using this port.")
        if port == other_port:
            return dashboard(csrf, "The staff web and camera POST ports must be different.", 409)
        destination = str(request.url.replace(port=port, path="/admin", query="")) if move_admin else "/admin"
        restarting = True
        try:
            await run_in_threadpool(change_port, port)
        except OSError:
            restarting = False
            return dashboard(csrf, "PlateLog could not open that port. Choose another port.", 409)
        except Exception:
            restarting = False
            raise
        return restart_response(destination)

    @app.post("/admin/web-port")
    async def save_web_port(request: Request):
        return await save_port(request, "staff web", web_port, camera_port, change_web_port, move_admin=True)

    @app.post("/admin/camera-port")
    async def save_camera_port(request: Request):
        return await save_port(request, "camera POST", camera_port if camera_online else None, web_port,
                               change_camera_port, move_admin=False)

    def register_sign_out_form(request: Request) -> str:
        if csrf := getattr(request.state, "staff_register_csrf", None):
            return (f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}">'
                    '<button type="submit">Sign out</button></form>')
        return ""

    return require_register, register_sign_out_form


def make_preview(data_dir: Path, allowed_host: str, display_zone: timezone | ZoneInfo = timezone.utc,
                 *, persistent: bool = False, allow_http_admin: bool = True,
                 restart: Callable[[], None] | None = None, web_port: int | None = None,
                 change_web_port: Callable[[int], None] | None = None, camera_port: int | None = None,
                 camera_online: bool = False, change_camera_port: Callable[[int], None] | None = None) -> FastAPI:
    if allowed_host != "*" and not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", allowed_host):
        raise ValueError("Expected a hostname, IPv4 address, or '*' for --allowed-host")
    store = Store(data_dir)
    if persistent and store.setting("admin_password") is None:
        store.initialize_admin_password(initial_admin_verifier())

    async def supervise_retention():
        while True:
            try:
                await run_in_threadpool(store.apply_retention)
            except Exception:
                LOG.exception("PlateLog retention failed")
            await asyncio.sleep(3600)

    async def supervise_csv():
        while True:
            try:
                await run_in_threadpool(export_due_report, store, display_zone)
            except Exception:
                LOG.exception("PlateLog daily CSV export failed")
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(_app):
        tasks = [asyncio.create_task(job()) for job in (supervise_retention, supervise_csv)] if persistent else []
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", allowed_host], www_redirect=False)
    require_register, register_sign_out_form = admin_routes(
        app, store, persistent, allow_http_admin, restart, web_port, change_web_port, display_zone,
        camera_port, camera_online, change_camera_port,
    )
    signed_in = [Depends(require_register)]

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

    def register_zone() -> timezone | ZoneInfo:
        saved_zone = store.setting("display_timezone") if persistent else None
        return ZoneInfo(saved_zone) if saved_zone else display_zone

    def filters_for_request(plate: str = Query("", max_length=128), camera: str = Query("", max_length=128),
                            start: str = Query("", max_length=16), end: str = Query("", max_length=16)) -> RegisterFilters:
        zone = register_zone()
        lower = time_bound(start, zone)
        upper = time_bound(end, zone, end=True)
        if lower is not None and upper is not None and (start > end or lower >= upper):
            raise HTTPException(400, "To must be the same minute as From or later.")
        return RegisterFilters(plate.strip(), camera, start, end, lower, upper, zone)

    def filter_params(filters: RegisterFilters) -> dict[str, str]:
        return {key: value for key in ("plate", "camera", "start", "end") if (value := getattr(filters, key))}

    def clock_header(zone: timezone | ZoneInfo) -> str:
        last_read = store.last_received_ms()
        server_ms = time.time_ns() // 1_000_000

        def clock_time(key: str, value: int) -> str:
            local = datetime.fromtimestamp(value / 1000, zone)
            return f'<time data-{key} datetime="{local.isoformat()}">{local.strftime("%d/%m/%Y, %H:%M:%S")}</time>'

        last_time = clock_time("last-read", last_read) if last_read is not None else '<time data-last-read hidden></time>'
        hide_empty = " hidden" if last_read is not None else ""
        zone_label = escape(str(zone))
        return f'''<dl id="register-clock" class="register-clock" title="{zone_label}"
            data-zone="{zone_label}" data-server-ms="{server_ms}">
            <div><dt>System time</dt><dd>{clock_time("system-time", server_ms)}</dd></div>
            <div><dt>Last read received</dt><dd>{last_time}<span data-no-reads{hide_empty}>No reads yet</span></dd></div></dl>'''

    @app.get("/register/status", dependencies=signed_in)
    def register_status():
        last_read = store.last_received_ms()
        return {"server_ms": time.time_ns() // 1_000_000, "last_read_ms": last_read, "zone": str(register_zone())}

    @app.get("/", response_class=HTMLResponse, dependencies=signed_in)
    def register(request: Request, filters: Annotated[RegisterFilters, Depends(filters_for_request)], page: int = Query(1, ge=1)):
        options = ['<option value="">All cameras</option>']
        for key in sorted(set(CAMERAS) | store.passage_cameras()):
            selected = " selected" if filters.camera == key else ""
            options.append(f'<option value="{escape(key)}"{selected}>{escape(CAMERA_NAMES.get(key, key))}</option>')
        controls = f'''<form id="register-search" action="/" method="get"><label>Plate <input type="search" name="plate" maxlength="128" value="{escape(filters.plate)}" autocomplete="off" spellcheck="false"></label>
            <label>Camera <select name="camera">{"".join(options)}</select></label>
            <label>From <input type="datetime-local" name="start" value="{escape(filters.start)}"></label>
            <label>To <input type="datetime-local" name="end" value="{escape(filters.end)}"></label>
            <button type="submit">Search</button><button type="submit" formaction="/register/export" data-export>Export Excel</button>
            <button type="submit" formaction="/register/export" data-export name="photos" value="false">Excel without photos</button>
            <a href="/" data-clear-search>Clear</a></form>'''
        toggles = "".join(f'<label><input type="checkbox" id="show-{key}" data-column-toggle="{key}" checked> {key.title()}</label>'
                          for key in VEHICLE_COLUMNS)
        toolbar = f'<div class="toolbar">{controls}<fieldset><legend>Columns</legend>{toggles}</fieldset></div>'
        return register_page(request, "Register", register_sign_out_form(request) + toolbar +
                             '<p id="search-status" role="status"></p>' +
                             f'<div id="register-results">{render_results(filters, page)}</div>',
                             header_extra=clock_header(filters.zone))

    def pagination(filters: RegisterFilters, page: int, last: int, position: str) -> str:
        if last < 2:
            return ""
        start = ((page - 1) // REGISTER_PAGE_LINKS) * REGISTER_PAGE_LINKS + 1
        stop = min(start + REGISTER_PAGE_LINKS, last + 1)
        links = [page_link(filters, page - 1, "Previous")] if page > 1 else []
        links.extend(page_link(filters, index, str(index), label=f"Page {index}", current=index == page)
                     for index in range(start, stop))
        if page < last:
            links.append(page_link(filters, page + 1, "Next"))
        return f'<nav class="register-pages" data-pager="{position}" aria-label="Passage pages ({position})">{"".join(links)}</nav>'

    def page_link(filters: RegisterFilters, page: int, text: str, *, label: str = "", current: bool = False) -> str:
        params = filter_params(filters)
        if page > 1:
            params["page"] = str(page)
        href = "/?" + escape(urlencode(params)) if params else "/"
        attributes = f' aria-label="{label}"' if label else ""
        if current:
            attributes += ' aria-current="page"'
        return f'<a href="{href}" data-page="{page}"{attributes}>{text}</a>'

    def render_results(filters: RegisterFilters, page: int) -> str:
        rows, total, page = store.passage_page(filters.plate, filters.camera, page, REGISTER_PAGE_SIZE,
                                              start_ms=filters.start_ms, end_ms=filters.end_ms)
        if not rows:
            return '<div data-register-page="1"><p class="register-live muted">Live updates</p><p role="status">No matching passages.</p></div>'
        first = (page - 1) * REGISTER_PAGE_SIZE + 1
        last = (total + REGISTER_PAGE_SIZE - 1) // REGISTER_PAGE_SIZE
        caption = f"Showing {first}–{first + len(rows) - 1} of {total:,} passages · {escape(str(filters.zone))}"
        live = "Live updates" if page == 1 else page_link(filters, 1, "Return to live")
        top = f'<div class="register-page-bar"><span class="register-live muted">{live}</span>{pagination(filters, page, last, "top")}</div>'
        bottom = f'<div class="register-page-bar">{pagination(filters, page, last, "bottom")}</div>' if last > 1 else ""
        result = []
        for row in rows:
            link = f"/passages/{row['id']}"
            plate_text = escape(row["plate"])
            photo_label = "Image expired" if row["photo_expired"] else "No image"
            photo = f'<span class="photo no-photo muted">{photo_label}</span>'
            if row["photo_id"]:
                photo = (f'<a href="{link}"><img class="photo" src="/images/{row["photo_id"]}" '
                         f'alt="Vehicle image for {plate_text}" loading="lazy" decoding="async"></a>')
            capture = datetime.fromtimestamp(row["capture_ms"] / 1000, filters.zone)
            date = capture.strftime("%d/%m/%Y")
            time = capture.strftime("%H:%M:%S")
            cells = "".join(f'<td data-column="{key}">{escape(row[key]) if row[key] else "—"}</td>'
                            for key in VEHICLE_COLUMNS)
            result.append(f'''<tr><td>{photo}</td><td class="registration"><a href="{link}">{plate_text}</a></td>
                <td>{date}</td><td><time datetime="{capture.isoformat()}" title="{capture.strftime('%Z %z')}">{time}</time></td>{cells}</tr>''')
        headings = "".join(f'<th scope="col" data-column="{key}">{key.title()}</th>' for key in VEHICLE_COLUMNS)
        return f'''<div data-register-page="{page}">{top}<p id="register-summary" class="muted">{caption}</p>
            <div class="scroll"><table aria-labelledby="register-summary">
            <thead><tr><th scope="col">Image</th><th scope="col">Reg</th><th scope="col">Date</th><th scope="col">Time</th>{headings}</tr></thead>
            <tbody>{"".join(result)}</tbody></table></div>{bottom}</div>'''

    @app.get("/register/results", response_class=HTMLResponse, dependencies=signed_in)
    def register_results(filters: Annotated[RegisterFilters, Depends(filters_for_request)], page: int = Query(1, ge=1)):
        return render_results(filters, page)

    @app.get("/register/export", dependencies=signed_in)
    def export_register(request: Request, filters: Annotated[RegisterFilters, Depends(filters_for_request)],
                        photos: bool = Query(True)):
        def reject(message: str, *, photo_fallback: bool = False, busy: bool = False) -> HTMLResponse:
            params = filter_params(filters)
            back = "/?" + escape(urlencode(params)) if params else "/"
            links = f'<a href="{back}">Back to filtered register</a>'
            if photo_fallback:
                no_photos = escape(urlencode({**params, "photos": "false"}))
                links = f'<a href="/register/export?{no_photos}">Export without photos</a> · ' + links
            content = f'<section><h2>Excel export</h2><p>{escape(message)}</p><p>{links}</p></section>'
            return HTMLResponse(register_page(request, "Export", content), status_code=429 if busy else 400,
                                headers={"Retry-After": "5"} if busy else None)

        if not _excel_export_lock.acquire(blocking=False):
            return reject("Another Excel export is being prepared. Wait a moment and try again.", busy=True)
        try:
            rows = store.export_passages(filters.plate, filters.camera, REPORT_LIMIT,
                                         start_ms=filters.start_ms, end_ms=filters.end_ms)
            if rows is None:
                return reject(f"More than {REPORT_LIMIT:,} passages match. Narrow From and To and try again.")
            report = excel_report(rows, filters, filters.zone, store.image_path, include_photos=photos)
        except ReportTooLarge as error:
            return reject(f"{error} Export without photos or narrow From and To.", photo_fallback=True)
        finally:
            _excel_export_lock.release()
        return Response(report,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="platelog-report.xlsx"'})

    @app.get("/passages/{passage_id}", response_class=HTMLResponse, dependencies=signed_in)
    def passage(request: Request, passage_id: str):
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
                photos.append(f'''<figure><img src="/images/{image['id']}" alt="Vehicle photo">
                    <figcaption class="muted">{state} packet · {read} · {captured}</figcaption></figure>''')
        no_photo = "<p>Image expired.</p>" if detail["photo_expired"] else "<p>No photo received.</p>"
        return register_page(request, detail["plate"], heading + f'''<section><h2>Photos</h2>{"".join(photos) or no_photo}</section>
            <section><h2>Packet history</h2><div class="scroll"><table><thead><tr><th>Event</th><th>Plate read</th><th>Direction</th><th>Captured (UTC)</th></tr></thead>
            <tbody>{"".join(history)}</tbody></table></div></section>''')

    @app.get("/images/{image_id}", dependencies=signed_in)
    def image(image_id: str):
        path = store.image_path(image_id) if re.fullmatch(ID_PATTERN, image_id) else None
        if path is None:
            raise HTTPException(404, "Image not found")
        return FileResponse(path, media_type="image/jpeg")

    return app
