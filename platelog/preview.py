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

from .admin_ui import CSS as ADMIN_CSS, JS as ADMIN_JS, SECTIONS, admin_shell, field, help_tip
from .alarm_admin import alarm_activity, alarm_table, smtp_form
from .alarms import SECRET_PREFIX, parse_rule, run_due_alarms
from .audible import JS as AUDIBLE_JS, REGISTER_CONTROL, settings_form as audible_settings_form
from .credentials import (
    INITIAL_ADMIN_PREFIX,
    MIN_PASSWORD_LENGTH,
    initial_admin_verifier,
    password_matches,
    password_verifier,
)
from .daily_csv import check_report_folder, export_due_report, report_folder, report_schedule
from .diagnostics import DOWNLOAD_LOCK, RANGES as DIAGNOSTIC_RANGES, build_support_bundle, record_event, route_category
from .email_alerts import DEFAULT_SMTP, SMTP_SECRET_PREFIX, parse_smtp_settings, test_smtp_connection
from .import_review import review_content, review_url, skipped_csv
from .receiver import CAMERA_ID_PATTERN, CAMERAS, ID_PATTERN
from .register_stats import render_stats
from .registered_refresh import refresh_due_registrations, refresh_schedule, refresh_status, save_refresh_schedule
from .store import RegistrationChanged, Store
from .vehicle_import import IMPORT_BYTES, parse_plate_import

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
.alarm-admin { max-width: 70rem; }
.alarm-admin .alarm-rule-form { display: block; }
.alarm-rule-form textarea { box-sizing: border-box; width: 100%; min-width: 20rem; padding: 6px 8px;
    border: 1px solid #8190a0; border-radius: 5px; font: inherit; resize: vertical; overflow-wrap: anywhere; }
.alarm-rule-form code { overflow-wrap: anywhere; }
.alarm-credentials { display: grid; gap: 4px; min-width: 13rem; }
.alarm-credentials input { box-sizing: border-box; width: 100%; }
.alarm-actions { display: flex; gap: 8px; align-items: center; white-space: nowrap; }
.register-audio:not([hidden]), .sound-tests { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.register-audio span { font-size: .85rem; color: #4b5d70; }
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
#csv-reports .folder-result { padding-inline-start: 8px; border-inline-start: 3px solid #194b79; overflow-wrap: anywhere; }
#registered-vehicles .import-file { min-width: 0; }
#registered-vehicles [hidden] { display: none; }
#registered-vehicles [data-import-message]:empty { display: none; }
#registered-vehicles [data-import-preview] { padding: 12px; margin-top: 12px; border: 1px solid #d3dce5; background: white; }
#registered-refresh .source-path { flex: 1 1 20rem; min-width: 0; }
.import-samples { display: flex; flex-wrap: wrap; gap: 10px 28px; }
.import-samples ul { margin-block: 4px; max-height: 10rem; overflow-y: auto; }
.import-review { max-width: 70rem; }
.import-review .review-table { overflow-x: auto; }
.import-review table { min-width: 48rem; table-layout: fixed; }
.import-review :is(th, td) { white-space: normal; overflow-wrap: anywhere; }
.import-review th:nth-child(1) { width: 5rem; }
.import-review th:nth-child(2) { width: 3rem; }
.import-review th:nth-child(3) { width: 22%; }
.import-review th:nth-child(4) { width: 18%; }
.import-review .review-value { max-width: 24rem; white-space: pre-wrap; overflow-wrap: anywhere; }
.import-review form label { flex: 1 1 22rem; min-width: 0; max-width: 100%; }
.import-review select { box-sizing: border-box; width: 100%; min-width: 0; }
.import-review dl { grid-template-columns: max-content minmax(0, 1fr); }
.import-review dd { overflow-wrap: anywhere; }
.import-review .pagination { display: flex; align-items: center; gap: 16px; margin-block: 12px; }
.register-stats { margin: 0 0 14px; padding: 8px 12px; border: 1px solid #d3dce5; border-radius: 6px; background: white; }
.register-stats h2 { font-size: 1rem; }
.stats-metrics { display: flex; flex-wrap: wrap; gap: 6px clamp(24px, 5vw, 72px); margin: 5px 0 8px; }
.stats-metrics dt { font-size: .78rem; color: #4b5d70; }
.stats-metrics dd { margin: 0; font-weight: 700; line-height: 1.22; font-variant-numeric: tabular-nums; }
.stats-chart { display: grid; grid-template-columns: max-content minmax(0, 1fr); column-gap: 8px; }
.stats-chart svg { display: block; width: 100%; height: 56px; }
.stats-grid { stroke: #d3dce5; fill: none; }
.stats-line { stroke: #194b79; stroke-width: 2; stroke-linejoin: round; fill: none; }
.stats-x, .stats-y { display: flex; justify-content: space-between; color: #4b5d70; font-size: .72rem; line-height: 1.15; }
.stats-x { grid-column: 2; padding-top: 3px; }
.stats-y { flex-direction: column; align-items: end; }
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
tr[data-registered] { --registration-color: #161616; }
tr[data-registered="false"] { --registration-color: #a21b25; }
tr[data-registered] .photo { box-sizing: border-box; border: 2px solid var(--registration-color); }
tr[data-registered] .registration a, .registration-status { color: var(--registration-color); }
.registration-status { display: block; font-size: .75rem; }
.history-heading { white-space: normal; min-width: 7.5rem; }
.history-heading .history-label { display: inline-block; vertical-align: middle; line-height: 1.3; }
.history-heading .history-window { display: block; font-weight: 400; font-size: .75rem; }
.passage-history { font-variant-numeric: tabular-nums; }
.passage-history strong { font-size: 1.05rem; }
.history-unknown { display: block; color: #4b5d70; font-size: .75rem; }
.creator-hover { position: relative; }
.creator-tooltip { position: absolute; top: calc(100% + 6px); left: 0; z-index: 30;
    padding: 6px 10px; border-radius: 4px; color: white; background: #172638;
    box-shadow: 0 3px 10px #0003; font: normal .8rem/1.4 system-ui, sans-serif;
    letter-spacing: normal; white-space: nowrap; pointer-events: none;
    opacity: 0; visibility: hidden; transition: opacity 0s, visibility 0s; }
.creator-hover:hover > .creator-tooltip { opacity: 1; visibility: visible; transition-delay: 20s; }
td[data-column] { white-space: normal; overflow-wrap: anywhere; }
td[data-column="make"], td[data-column="colour"] { white-space: nowrap; overflow-wrap: normal; }
figure { margin: 12px 0; } figure img { display: block; width: auto; max-width: min(100%, 640px); height: auto; border-radius: 5px; }
main:has(#show-history:not(:checked)) [data-column="history"],
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
    const stats = document.querySelector('#register-stats-content');
    const status = document.querySelector('#search-status');
    let pending;
    let statsPending;
    let rendered = results.innerHTML;
    let renderedStats = stats.innerHTML;
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

    async function updateStats() {
        if (document.hidden) return;
        const selected = camera.value;
        if (statsPending?.camera === selected) return;
        statsPending?.request.abort();
        const request = new AbortController();
        statsPending = { request, camera: selected };
        try {
            const query = new URLSearchParams({ camera: selected });
            const response = await fetch(`/register/stats?${query}`, { signal: request.signal });
            if (!response.ok) return;
            const html = await response.text();
            if (!request.signal.aborted && selected === camera.value && html !== renderedStats) {
                stats.innerHTML = html;
                renderedStats = html;
            }
        } catch {} finally {
            if (statsPending?.request === request) statsPending = undefined;
        }
    }

    const filter = () => update(1);
    const refresh = () => update(1, { automatic: true });
    window.setInterval(refresh, 5000);
    window.setInterval(updateStats, 5000);
    document.addEventListener('visibilitychange', refresh);
    document.addEventListener('visibilitychange', updateStats);
    results.addEventListener('click', event => {
        const link = event.target.closest('a[data-page]');
        if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        update(Number(link.dataset.page), { pager: link.closest('[data-pager]')?.dataset.pager ?? 'top' });
    });

    plate.addEventListener('input', filter);
    for (const input of [camera, form.elements.start, form.elements.end]) input.addEventListener('change', filter);
    camera.addEventListener('change', updateStats);
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
        updateStats();
        plate.focus();
    });
}

const vehicleImport = document.querySelector('#vehicle-import');
if (vehicleImport) {
    const fileInput = vehicleImport.elements.plate_file;
    const modeInput = vehicleImport.elements.import_mode;
    const previewButton = vehicleImport.querySelector('button');
    const region = document.querySelector('#registered-vehicles');
    const message = region.querySelector('[data-import-message]');
    const preview = region.querySelector('[data-import-preview]');
    const confirmation = region.querySelector('[data-import-confirm]');
    const replaceCheck = confirmation.elements.confirm_replace;
    let reviewed;

    function resetPreview() {
        reviewed = undefined;
        preview.hidden = true;
        replaceCheck.checked = false;
    }

    for (const field of [fileInput, modeInput]) {
        field.addEventListener('change', () => {
            resetPreview();
            message.textContent = '';
        });
    }

    async function upload(action, selection) {
        const { file, mode, format, receipt } = selection;
        const query = new URLSearchParams({ mode, format, filename: file.name });
        const headers = { 'X-CSRF-Token': vehicleImport.dataset.csrf };
        if (receipt) headers['X-Import-Receipt'] = receipt;
        if (replaceCheck.checked) headers['X-Confirm-Replace'] = 'yes';
        const response = await fetch(`/admin/registered-vehicles/${action}?${query}`, {
            method: 'POST', headers, body: file,
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail);
        return result;
    }

    async function withUpload(action) {
        const apply = confirmation.querySelector('button');
        for (const field of [fileInput, modeInput, previewButton, apply, replaceCheck]) field.disabled = true;
        try {
            await action();
        } catch (error) {
            resetPreview();
            message.textContent = error.message || 'The upload failed. Try again.';
        } finally {
            for (const field of [fileInput, modeInput, previewButton, apply, replaceCheck]) field.disabled = false;
        }
    }

    function showSample(selector, texts) {
        const list = preview.querySelector(selector);
        list.replaceChildren(...texts.map(text => {
            const item = document.createElement('li');
            item.textContent = text;
            return item;
        }));
        list.parentElement.hidden = texts.length === 0;
    }

    vehicleImport.addEventListener('submit', event => {
        event.preventDefault();
        resetPreview();
        const file = fileInput.files[0];
        if (!file) return;
        const format = file.name.split('.').pop().toLowerCase();
        if (!['csv', 'xlsx'].includes(format)) {
            message.textContent = 'Choose a CSV or .xlsx file.';
            return;
        }
        const mode = modeInput.value;
        message.textContent = 'Reading plate list…';
        withUpload(async () => {
            const result = await upload('preview', { file, mode, format });
            reviewed = { file, mode, format, receipt: result.receipt };
            preview.querySelector('[data-import-summary]').textContent =
                `${result.added.toLocaleString()} added · ${result.existing.toLocaleString()} kept from file · ` +
                `${result.removed.toLocaleString()} removed · ${result.duplicates.toLocaleString()} duplicate registrations skipped · ` +
                `${result.total.toLocaleString()} plates after import`;
            const report = result.import_report;
            preview.querySelector('[data-import-notes]').textContent = report.format === 'cardholders'
                ? `${report.excluded_rows.toLocaleString()} non-authorised rows excluded · ` +
                  `${report.skipped_entries.toLocaleString()} invalid or empty entries skipped · ` +
                  `${report.spaces_removed.toLocaleString()} entries had spaces removed. ` +
                  'Check that commas separate different registrations. ' +
                  (report.shared_plates ? `${report.shared_plates.toLocaleString()} plates appear under both Yes and No; an authorised Yes entry takes priority.` : '')
                : '';
            showSample('[data-import-sample]', result.sample.map(row => `${row.action}: ${row.plate}`));
            showSample('[data-import-removals]', result.removed_sample);
            showSample('[data-import-warnings]', report.warnings);
            replaceCheck.required = mode === 'replace';
            replaceCheck.parentElement.hidden = mode !== 'replace';
            preview.hidden = false;
            message.textContent = 'Preview ready. Review the changes before applying.';
        });
    });

    confirmation.addEventListener('submit', event => {
        event.preventDefault();
        if (!reviewed) return;
        message.textContent = 'Importing plates…';
        withUpload(async () => {
            const result = await upload('apply', reviewed);
            region.querySelector('[data-registered-count]').textContent = result.total.toLocaleString();
            message.textContent = `Import complete. ${result.added.toLocaleString()} added, ${result.removed.toLocaleString()} removed.` +
                (result.import_report.skipped_entries ? ` ${result.import_report.skipped_entries.toLocaleString()} invalid or empty entries were skipped. Open Skipped entries for the full details.` : '');
            vehicleImport.reset();
            resetPreview();
        });
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
    tab = "admin" if title in {"Admin", "Alarm rules", "Skipped entries"} else "register"
    register_current = ' aria-current="page"' if tab == "register" else ""
    admin_current = ' aria-current="page"' if tab == "admin" else ""
    admin_link = f'<a href="/admin"{admin_current}>Admin</a>' if show_admin else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(title)} | PlateLog</title><link rel="stylesheet" href="/preview.css"><script defer src="/preview.js"></script>
    <script defer src="/audible.js"></script><script defer src="/admin.js"></script></head>
    <body{' class="admin-page"' if tab == 'admin' else ''}><main><header><div class="header-top"><div><h1 class="creator-hover">PlateLog<span class="creator-tooltip" aria-hidden="true">🧠 by Liam.</span></h1><p>Vehicle passage log</p></div>{header_extra}</div>
    <nav class="tabs" aria-label="PlateLog"><a href="/" class="creator-hover"{register_current}>Plate log<span class="creator-tooltip" aria-hidden="true">🧠 by Liam.</span></a>
    {admin_link}</nav></header>{content}</main></body></html>"""


def register_page(request: Request, title: str, content: str, *, header_extra: str = "") -> str:
    return page(title, content, show_admin=not getattr(request.state, "staff_register_csrf", None),
                header_extra=header_extra)


def set_admin_password(store: Store, password: str) -> None:
    store.set_setting("admin_password", password_verifier(password), "Administrator password set")


def require_form_origin(request: Request) -> None:
    expected = f"{request.url.scheme}://{request.headers.get('host')}"
    if request.headers.get("origin") != expected:
        raise HTTPException(403, "Submit the form from the PlateLog Admin page.")


async def admin_form(request: Request, limit: int = 4096) -> dict[str, str]:
    require_form_origin(request)
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
        camera_url = urlencode({"section": "axis-cameras", "camera": row["id"]})
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
                field("camera-id", "Camera ID", f'<input name="camera_id" value="{draft_id}" maxlength="64" placeholder="entry" required>',
                      "A unique name for this source camera. Use lowercase letters, digits, dashes or underscores. The ID forms part of its POST path."))
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
    auth = field("camera-auth", "POST authentication", f'<select name="mode">{options}</select>',
                 "Disabled rejects camera events. HTTP Basic requires the camera to send a matching username and password. No authentication accepts events from any client that can reach this endpoint.")
    user = field("camera-user", "Username", f'<input name="username" maxlength="128" autocomplete="off" value="{escape(username)}">',
                 "Enter the same username in the camera's push event settings. Do not use spaces or colons.")
    password_tip = help_tip("camera-password", "Password", "Use at least 14 characters. Set the same password on the source camera. Leave blank to retain the saved password for the same username.")
    return f'''<div id="camera-editor" class="camera-editor"><h4>{title}</h4>{path}{alert}
    <form class="camera-form" method="post" action="/admin/cameras#camera-editor"><input type="hidden" name="csrf" value="{csrf}">
        {identity}{auth}
        <div class="camera-auth-fields">
            {user}<div class="camera-password field"><div class="field-label"><label for="{password_id}">Password</label>{password_tip}</div>
            <input id="{password_id}" type="password" name="password" autocomplete="new-password"
                minlength="{MIN_PASSWORD_LENGTH}" aria-describedby="{hint_id}"{invalid}>
            {hint}</div>
        </div>
        <div class="camera-actions"><button type="submit">{'Save camera' if camera_id else 'Add camera'}</button>
        <a href="/admin?section=axis-cameras">Cancel</a></div></form></div>'''


def csv_schedule_form(store: Store, csrf: str, zone: str, *, draft: dict | None = None,
                      folder_result: str = "") -> str:
    schedule = draft if draft is not None else report_schedule(store)
    stored_status = store.setting("daily_csv_status")
    last = json.loads(stored_status) if stored_status else None
    result = "<p>No export has run yet.</p>"
    if last:
        label = "Last export failed" if last["result"] == "error" else "Last successful export"
        result = (f'<p>{label} on {escape(last["attempted_at"])}. {escape(last["message"])}</p>'
                  f'<p><code>{escape(last["file"])}</code></p>')
    folder_notice = f'<p class="folder-result" role="status">{escape(folder_result)}</p>' if folder_result else ""
    run_time = field("csv-run", "Run time", f'<input type="time" name="run_time" value="{escape(schedule["run_time"])}" required>',
                     f"The daily export runs in {zone} time and covers the previous local calendar day.")
    folder = field("csv-folder", "Destination folder", f'<input name="folder" value="{escape(schedule["folder"])}" maxlength="1024" spellcheck="false">',
                   "Use a full server path or Windows UNC share. The PlateLog service account needs write access. Test folder writes, reads and removes a temporary file without changing the schedule.", wide=True)
    return f'''<section id="csv-reports"><h3>Daily CSV report</h3>
        <p>Exports the previous calendar day in {escape(zone)}, with a header row and one row per passage.</p>
        <form class="form-grid" method="post" action="/admin/csv-schedule"><input type="hidden" name="csrf" value="{csrf}">
        <label class="enable field-wide"><input type="checkbox" name="enabled" value="yes" {'checked' if schedule['enabled'] else ''}> Enable daily CSV export</label>
        {run_time}{folder}<p class="form-note">Files are named <code>platelog-YYYY-MM-DD.csv</code>.</p>
        <div class="form-actions"><button type="submit">Save CSV schedule</button>
        <button class="secondary" type="submit" name="test_folder" value="yes" formaction="/admin/csv-schedule#csv-reports" formnovalidate>Test folder</button></div>
        </form>{folder_notice}{result}</section>'''


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


def registered_refresh_form(store: Store, csrf: str, zone: str, *, draft: dict | None = None,
                            notice: str = "") -> str:
    schedule = draft if draft is not None else refresh_schedule(store)
    status = refresh_status(store)
    attempt = status.get("last_attempt", {})
    success = status.get("last_success")

    def run_time(entry: dict) -> str:
        recorded = datetime.fromisoformat(entry["attempted_at"])
        return f'<time datetime="{recorded.isoformat()}">{recorded:%d/%m/%Y %H:%M:%S %z}</time>'

    result = "<p>No successful refresh yet.</p>"
    if success:
        result = (f'<p>Last successful refresh {run_time(success)}. '
                  f'{success["total"]:,} plates · {success["added"]:,} added · {success["removed"]:,} removed.</p>')
        report = success.get("import_report", {})
        if report.get("format") == "cardholders":
            result += (f'<p>{report["excluded_rows"]:,} non-authorised rows excluded · '
                       f'{report["skipped_entries"]:,} invalid or empty entries skipped · '
                       f'{report["spaces_removed"]:,} entries had spaces removed. '
                       'Check that commas separate different registrations.</p>')
            if report["shared_plates"]:
                result += (f'<p>{report["shared_plates"]:,} plates appear under both Yes and No. '
                           'An authorised Yes entry takes priority.</p>')
            if report["warnings"]:
                warnings = "".join(f'<li>{escape(warning)}</li>' for warning in report["warnings"])
                result += f'<details><summary>First 20 entries for review</summary><ul>{warnings}</ul></details>'
    if attempt.get("error"):
        result += (f'<p>Last refresh failed {run_time(attempt)}. {escape(attempt["error"])} '
                   'The current list was kept.</p>')
    if attempt.get("import_run"):
        result += f'<p><a href="{escape(review_url(attempt["import_run"]))}">View skipped entries for this refresh</a></p>'
    message = f'<p class="notice" role="status">{escape(notice)}</p>' if notice else ""
    source_field = field("refresh-source", "CSV file path", f'<input name="source" value="{escape(schedule["source"])}" maxlength="1024" spellcheck="false">',
                         "A full path on the server or a Windows UNC share readable by the service account. A successful refresh replaces the registered list; a failed refresh keeps the current list.", wide=True)
    time_field = field("refresh-time", "Refresh time", f'<input type="time" name="run_time" value="{escape(schedule["run_time"])}" required>',
                       f"Run once each day in {zone} time. Invalid or empty authorised entries are recorded in Skipped entries.")
    return f'''<section id="registered-refresh"><h4>Scheduled list refresh</h4>
        <p>Replace the registered list once a day from a CSV in {escape(zone)} time.</p>
        <form class="form-grid" method="post" action="/admin/registered-refresh#registered-refresh">
            <input type="hidden" name="csrf" value="{csrf}">
            <label class="enable field-wide"><input type="checkbox" name="enabled" value="yes" {'checked' if schedule["enabled"] else ''}> Enable scheduled refresh</label>
            {source_field}{time_field}<div class="form-actions"><button type="submit">Save refresh schedule</button></div></form>
        {message}{result}</section>'''


def registered_vehicles_form(csrf: str, count: int, refresh: str) -> str:
    upload = field("plate-file", "Plate list", '<input type="file" name="plate_file" accept=".csv,.xlsx" required>',
                   "Use a CSV or XLSX with a Plate column, or Vehicle and Cardholder Authorised columns. Keep registrations as Text in Excel to preserve leading zeros. Commas separate plates; spaces are removed; punctuation is skipped for review.")
    mode = field("import-mode", "Import mode", '<select name="import_mode"><option value="replace" selected>Replace entire list</option><option value="add">Add to existing list</option></select>',
                 "Add keeps existing registrations. Replace removes registrations absent from the file, after you preview and confirm the change.")
    return f'''<section id="registered-vehicles"><h3>Import list</h3>
        <p><strong data-registered-count>{count:,}</strong> registered plates ·
        <a href="/admin/registered-vehicles/template.csv">Download CSV template</a> ·
        <a href="/admin/import-review">Skipped entries</a></p>
        <p>Comma-separated registrations; spaces removed. Only authorised Yes rows are registered in cardholder exports.</p>
        <form class="form-grid" id="vehicle-import" data-csrf="{csrf}" method="post">{upload}{mode}
            <div class="form-actions"><button type="submit">Preview import</button></div></form>
        <p data-import-message role="status"></p>
        <div data-import-preview hidden>
            <h4>Review import</h4><p data-import-summary></p><p data-import-notes></p>
            <div class="import-samples"><div>First 20 file entries<ul data-import-sample></ul></div>
                <div hidden>First 20 removals<ul data-import-removals></ul></div>
                <div hidden>First 20 entries for review<ul data-import-warnings></ul></div></div>
            <form data-import-confirm method="post">
                <label class="enable" hidden><input type="checkbox" name="confirm_replace" value="yes">
                Remove all currently registered plates missing from this file</label>
                <button type="submit">Apply import</button></form>
        </div>{refresh}</section>'''


def admin_routes(app: FastAPI, store: Store, persistent: bool, allow_http_admin: bool,
                 restart: Callable[[], None] | None, web_port: int | None,
                 change_web_port: Callable[[int], None] | None, display_zone: timezone | ZoneInfo,
                 camera_port: int | None, camera_online: bool, change_camera_port: Callable[[int], None] | None,
                 pilot_camera_transport: str | None):
    sessions: dict[str, tuple[float, str, str]] = {}
    staff_sessions: dict[str, tuple[float, str, str]] = {}
    login_attempts: list[float] = []
    staff_attempts: list[float] = []
    password_check = asyncio.Lock()
    import_secret = secrets.token_bytes(32)
    restarting = False

    def transport_ok(request: Request) -> bool:
        return allow_http_admin or request.url.scheme == "https"

    def register_access() -> dict:
        config = store.register_access()
        if pilot_camera_transport is not None and not config["revision"]:
            config["required"] = True
        return config

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
        config = register_access()
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
        raise HTTPException(401, "Sign in to view the plate log.")

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
        if not register_access()["verifier"]:
            return HTMLResponse(page("Staff sign-in", '<section><h2>Staff sign-in</h2>'
                                     '<p>An administrator needs to set the staff password in <a href="/admin">Admin</a>.</p>'
                                     '</section>'), status_code=status)
        notice = f'<p role="alert">{escape(message)}</p>' if message else ""
        return HTMLResponse(page("Staff sign-in", f'''<section><h2>Staff sign-in</h2>{notice}
            <form method="post" action="/login"><input type="hidden" name="next" value="{escape(destination)}">
            <label>Staff password <input type="password" name="password" autocomplete="current-password" required></label>
            <button type="submit">Sign in</button></form></section>'''), status_code=status)

    @app.get("/login")
    def register_login(request: Request, next: str = "/"):
        destination = register_destination(next)
        config = register_access() if persistent else None
        if not config or not config["required"] or admin_csrf(request) or staff_csrf(request, config):
            return RedirectResponse(destination, status_code=303)
        if not transport_ok(request):
            return HTMLResponse(page("Staff sign-in", "<p>This server requires HTTPS for staff sign-in.</p>"), status_code=403)
        return register_login_page(destination)

    @app.post("/login")
    async def register_sign_in(request: Request):
        fields = await admin_form(request, limit=12_288)
        destination = register_destination(fields.get("next", ""))
        config = register_access() if persistent else None
        if not config or not config["required"]:
            return RedirectResponse(destination, status_code=303)
        if not transport_ok(request):
            raise HTTPException(403, "Staff sign-in requires HTTPS.")
        if not config["verifier"]:
            return register_login_page(destination, status=403)
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
        config = register_access() if persistent else None
        csrf = staff_csrf(request, config) if config else None
        if not csrf or not hmac.compare_digest(csrf.encode(), fields.get("csrf", "").encode()):
            raise HTTPException(403, "Reload the plate log before signing out.")
        return sign_out(request, "/", STAFF_COOKIE, staff_sessions)

    def show(content: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(page("Admin", f'<div class="admin admin-login"><h2>Admin</h2>{content}</div>'), status_code=status)

    def login_page(message: str = "", status: int = 200) -> HTMLResponse:
        notice = f'<p role="alert">{escape(message)}</p>' if message else ""
        return show(notice + '''<form method="post" action="/admin/login">
            <label>Administrator password <input type="password" name="password" autocomplete="current-password" required></label>
            <button type="submit">Sign in</button></form>''', status)

    def password_form(csrf: str) -> str:
        current = field("admin-current", "Current password", '<input type="password" name="current_password" autocomplete="current-password" required>',
                        "Enter the administrator password used for this sign-in.")
        new = field("admin-new", "New password", '<input type="password" name="new_password" autocomplete="new-password" minlength="14" required>',
                    "Use at least 14 characters and no more than 1,024 UTF-8 bytes. Changing the password ends all administrator sessions.")
        confirm = field("admin-confirm", "Confirm new password", '<input type="password" name="confirm_password" autocomplete="new-password" minlength="14" required>')
        return f'''<section><h3>Change administrator password</h3>
            <p>Use at least 14 characters.</p><form class="form-grid" method="post" action="/admin/password">
            <input type="hidden" name="csrf" value="{csrf}">{current}{new}{confirm}
            <div class="form-actions"><button type="submit">Change password</button></div></form></section>'''

    def initial_password_page(csrf: str, message: str = "Choose a new password to unlock Admin.", status: int = 200) -> HTMLResponse:
        return show(f'<p role="status">{escape(message)}</p>{password_form(csrf)}', status)

    def dashboard(csrf: str, message: str = "", status: int = 200, *, section: str = "axis-cameras",
                  camera_selection: str | None = None, camera_error: CameraFormError | None = None, camera_saved: str = "",
                  csv_draft: dict | None = None, folder_result: str = "",
                  refresh_draft: dict | None = None, refresh_notice: str = "") -> HTMLResponse:
        if csv_draft is not None or folder_result:
            section = "csv-reports"
        elif refresh_draft is not None or refresh_notice:
            section = "registered-vehicles"
        elif camera_selection is not None or camera_error or camera_saved:
            section = "axis-cameras"
        section = section if section in SECTIONS and section != "alarms" else "axis-cameras"
        zone = store.setting("display_timezone") or str(display_zone)
        content, actions = "", ""
        if section == "axis-cameras":
            rows = store.cameras()
            if camera_error:
                camera_selection = camera_error.camera_id
            selected = next((row for row in rows if row["id"] == camera_selection), None)
            editor = ""
            if selected is not None:
                editor = camera_form(csrf, selected["id"], selected["mode"], selected["username"], error=camera_error)
            elif camera_selection == "" or camera_error:
                editor = camera_form(csrf, error=camera_error)
            message = f"Saved camera {camera_saved}." if camera_saved else message
            listener = f"Camera listener: port {camera_port}." if camera_online else f"Camera listener offline on port {camera_port}."
            if pilot_camera_transport == "off":
                listener = "The Axis camera listener is disabled. Enable it at launch before connecting cameras."
            elif pilot_camera_transport:
                listener = f"Camera listener: {pilot_camera_transport.upper()} on port {camera_port}."
            hint = help_tip("camera-post-path", "Camera POST path", "Combine the server address and camera listener port with the POST path in this table. Configure HTTP POST in AXIS License Plate Verifier. The source camera uses the authentication shown in its row.")
            content = f'<p>{listener} {hint}</p>{camera_table(rows, camera_selection)}{editor}'
            actions = '<a class="camera-add" href="/admin?section=axis-cameras&amp;camera=#camera-editor">Add camera</a>'
        elif section == "registered-vehicles":
            refresh = registered_refresh_form(store, csrf, zone, draft=refresh_draft, notice=refresh_notice)
            content = registered_vehicles_form(csrf, store.registered_count(), refresh)
        elif section == "audible-alerts":
            content = audible_settings_form(csrf, store.audible_settings())
        elif section == "csv-reports":
            content = csv_schedule_form(store, csrf, zone, draft=csv_draft, folder_result=folder_result)
        elif section == "access":
            access = register_access()
            note = "Leave blank to keep the current staff password." if access["verifier"] else "Use at least 14 characters."
            staff = field("staff-password", "New staff password", '<input type="password" name="password" minlength="14" autocomplete="new-password">',
                          "The staff password opens the plate log only. The administrator password opens the plate log and Admin. Leave this blank to keep the saved staff password.")
            content = f'''<section id="register-access"><h3>Plate log access</h3><form class="form-grid" method="post" action="/admin/register-access">
                <input type="hidden" name="csrf" value="{csrf}">
                <label class="enable field-wide"><input type="checkbox" name="required" value="yes" {'checked' if access['required'] else ''}>
                Require staff sign-in to view the plate log</label>{staff}<p class="form-note">{note}</p>
                <div class="form-actions"><button type="submit">Save plate log access</button></div></form></section>{password_form(csrf)}'''
        elif section == "retention":
            retention = store.retention()
            counts, _ = store.admin_overview()
            last = store.setting("retention_last_run")
            storage = ''.join(f'<div><dt>{name}</dt><dd>{value}</dd></div>' for name, value in (
                ("Passages", f'{counts["passages"]:,}'), ("Photos", f'{counts["photos"]:,}'),
                ("Events", f'{counts["packets"]:,}'), ("Photo storage", f'{counts["photo_bytes"] / 1_048_576:.2f} MiB')))
            days = field("passage-days", "Keep passages (days)", f'<input type="number" name="passage_days" min="1" max="3650" value="{retention["passage_days"]}" required>',
                         "Passages include the plate, time and event metadata. Default: 360 days. Cleanup permanently removes older passages.")
            photos = field("photo-days", "Keep photos (days)", f'<input type="number" name="photo_days" min="1" max="3650" value="{retention["photo_days"]}" required>',
                           "Default: 90 days. Photos may be removed before their passages. This cannot be longer than passage retention.")
            content = f'''<section><h3>Storage</h3><dl class="storage-summary">{storage}</dl>
                <p class="muted">Last cleanup (UTC): {timestamp(int(last)) if last else 'None yet'}</p></section>
                <section><h3>Automatic cleanup</h3><p>Runs at startup and hourly. Expired passages and photos are permanently removed.</p>
                <form class="form-grid" method="post" action="/admin/settings"><input type="hidden" name="csrf" value="{csrf}">
                <label class="enable field-wide"><input type="checkbox" name="enabled" value="yes" {'checked' if retention['enabled'] else ''}>Enable automatic cleanup</label>
                {days}{photos}<div class="form-actions"><button type="submit">Save retention settings</button></div></form></section>
                <section class="danger-zone"><h3>Purge expired data now</h3><p>Uses the saved retention limits. This cannot be undone in PlateLog.</p>
                <form method="post" action="/admin/purge"><input type="hidden" name="csrf" value="{csrf}">
                <input type="hidden" name="passage_days" value="{retention['passage_days']}"><input type="hidden" name="photo_days" value="{retention['photo_days']}">
                <label class="enable"><input type="checkbox" name="confirm" value="yes" required>
                Permanently delete passages older than {retention['passage_days']} days and photos older than {retention['photo_days']} days</label>
                <button class="danger" type="submit">Purge now</button></form></section>'''
        elif section == "diagnostics":
            choices = ''.join(f'<option value="{key}">{label}</option>' for key, label in DIAGNOSTIC_RANGES.items())
            period = field("diagnostic-days", "History to include", f'<select name="days">{choices}</select>',
                           "Choose a rolling 24, 48 or 72 hours, or all available retained history. Detailed logs keep up to 30 days or 8 MiB. Older Admin, import and alarm outcomes are included where retained. The bundle states what was available and any limits.")
            content = f'''<section><h3>Support bundle</h3><p>Download a ZIP to copy to a USB drive or share for support. Nothing is uploaded automatically.</p>
                <form class="form-grid" method="post" action="/admin/diagnostics/download"><input type="hidden" name="csrf" value="{csrf}">
                {period}<div class="form-actions"><button type="submit">Download diagnostics ZIP</button></div></form>
                <p class="form-note">Includes system versions, safe configuration summaries, job outcomes and structured errors with code locations.
                Excludes passwords, vehicle registrations, photos, raw payloads, email addresses and file paths.</p>
                <p class="muted">Detailed logging starts with this version. Earlier faults may only have a saved job result or Admin activity entry.</p></section>
                <section><h3>If the web page will not start</h3><p>IT can run the standard-library exporter directly from the extracted package folder, without starting PlateLog.</p>
                <pre><code>python -I -B app/platelog/diagnostics.py --data-dir "C:\\ProgramData\\PlateLog\\pilot-data" --days 1 --output "C:\\Staging\\platelog-support.zip"</code></pre>
                <p class="muted">Use the active data folder and an unused output filename. The exporter also works if the main database is missing or unreadable.</p></section>'''
        elif section == "system":
            timezone_field = field("timezone", "Time zone", f'<input name="timezone" maxlength="128" value="{escape(zone)}" required>',
                                   "Use an IANA time zone, such as Australia/Sydney. This controls displayed times and daily schedules. It does not change the Windows or camera clock.")
            history_field = field("history-days", "Unregistered history (days)",
                                  f'<input type="number" name="days" min="1" max="3650" value="{store.unregistered_history_days()}" required>',
                                  "Default: 30 days. For currently unregistered vehicles, History shows earlier unregistered passages across all cameras in the rolling window ending at that read, excluding the current passage. Registered vehicles show a dash. Later CSV imports do not rewrite the recorded status. Counts use retained passage records, independently of photo retention.")
            content = f'''<section><h3>Display</h3><form class="form-grid" method="post" action="/admin/timezone">
                <input type="hidden" name="csrf" value="{csrf}">{timezone_field}
                <div class="form-actions"><button type="submit">Save time zone</button></div></form>
                <form class="form-grid" method="post" action="/admin/unregistered-history"><input type="hidden" name="csrf" value="{csrf}">
                {history_field}<div class="form-actions"><button type="submit">Save history window</button></div></form>
                <p class="muted">Passage retention is currently {store.retention()['passage_days']} days. Earlier records without a saved classification are shown as unknown.</p></section>
                <section><h3>Listener ports</h3><p>Staff web: <strong>{web_port}</strong> · Camera POST: <strong>{camera_port or 'Disabled'}</strong></p>'''
            for kind, port, change, endpoint, label in (
                    ("staff", web_port, change_web_port, "web-port", "Staff web port"),
                    ("camera", camera_port, change_camera_port, "camera-port", "Camera POST port")):
                if restart and change:
                    port_field = field(f"{kind}-port", label, f'<input type="number" name="port" min="1" max="65535" value="{port or ""}" required>',
                                       "Allow the new TCP port in the firewall before applying it. Staff and camera ports must differ. Changing this restarts PlateLog and ends browser sessions.")
                    content += f'''<form class="form-grid" method="post" action="/admin/{endpoint}"><input type="hidden" name="csrf" value="{csrf}">
                        {port_field}<div class="form-actions"><button type="submit">Save {kind} port and restart</button></div></form>'''
            if not restart:
                content += '<p class="muted">Ports and restart are managed by the server deployment configuration.</p>'
            content += '</section>'
            if restart:
                content += f'''<section><h3>Service</h3><p>Restart interrupts live updates briefly and ends browser sessions.</p>
                    <form method="post" action="/admin/restart"><input type="hidden" name="csrf" value="{csrf}">
                    <label class="enable"><input type="checkbox" name="confirm" value="yes" required>Confirm restart</label>
                    <button class="secondary" type="submit">Restart PlateLog</button></form></section>'''
            _, audit = store.admin_overview()
            changes = ''.join(f'<li>{escape(entry["activity"])}<time>{timestamp(entry["at_ms"])}</time></li>' for entry in audit)
            content += f'<section><h3>Recent admin activity (UTC)</h3>{f"<ul class=activity-list>{changes}</ul>" if changes else "<p>No activity yet.</p>"}</section>'
        shell = admin_shell(section, content, csrf, message=message, error=status >= 400, actions=actions)
        return HTMLResponse(page("Admin", shell), status_code=status)

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request, saved: bool = False, password_changed: bool = False, csv_saved: bool = False,
              access_saved: bool = False, camera: str | None = None, camera_saved: str = "",
              refresh_saved: bool = False, sounds_saved: bool = False, section: str = ""):
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
            message = ("Plate log access saved." if access_saved else "CSV schedule saved." if csv_saved else
                       "Retention settings saved." if saved else "Sound settings saved." if sounds_saved else "")
            refresh_notice = "Refresh schedule saved." if refresh_saved else ""
            active = section or ("access" if access_saved else "csv-reports" if csv_saved else
                                 "retention" if saved else "audible-alerts" if sounds_saved else
                                 "registered-vehicles" if refresh_saved else "axis-cameras")
            return dashboard(csrf, message, section=active, camera_selection=camera, camera_saved=camera_saved,
                             refresh_notice=refresh_notice)
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

    @app.post("/admin/diagnostics/download")
    async def download_diagnostics(request: Request):
        csrf, fields = await authorized_form(request)
        days = fields.get("days", "1")
        if days not in DIAGNOSTIC_RANGES:
            return dashboard(csrf, "Choose 1 day, 2 days, 3 days or all available history.", 400, section="diagnostics")
        if not DOWNLOAD_LOCK.acquire(blocking=False):
            return dashboard(csrf, "Another support bundle is being prepared. Try again shortly.", 429, section="diagnostics")
        try:
            runtime = {"staff_port": web_port, "camera_port": camera_port, "camera_online": camera_online,
                       "mode": "pilot" if pilot_camera_transport is not None else "preview", "https": request.url.scheme == "https"}
            data = await run_in_threadpool(build_support_bundle, store.database.parent, days, runtime=runtime)
        except (OSError, ValueError) as exc:
            record_event(store.database.parent, "request.failed", "admin", level="error", error=exc, route="admin")
            return dashboard(csrf, "The support bundle could not be created. Check free space and service permissions, or try a shorter period.", 500, section="diagnostics")
        finally:
            DOWNLOAD_LOCK.release()
        record_event(store.database.parent, "support.download", "admin", days=int(days) if days != "all" else 0, bytes=len(data))
        filename = f'platelog-support-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{days}.zip'
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})

    def alarm_page(csrf: str, selected: str | None, *, draft: dict[str, str] | None = None,
                   message: str = "", status: int = 200, view: str = "rules", smtp_draft: dict | None = None) -> HTMLResponse:
        cameras = store.cameras()
        rules, recent = store.alarm_overview()
        stored_zone = store.setting("display_timezone")
        zone = ZoneInfo(stored_zone) if stored_zone else display_zone
        add = ('<a class="camera-add" href="/admin/alarms?camera=#alarm-editor">Add rule</a>'
               if view != "smtp" and rules and len(rules) < len(cameras) else "")
        content = f'''<nav class="subtabs" aria-label="Alarm settings">
            <a href="/admin/alarms"{' aria-current="page"' if view != 'smtp' else ''}>Rules</a>
            <a href="/admin/alarms?view=smtp"{' aria-current="page"' if view == 'smtp' else ''}>Email delivery (SMTP)</a></nav>'''
        if view == "smtp":
            content += smtp_form(csrf, smtp_draft if smtp_draft is not None else store.smtp_settings())
        else:
            content += alarm_table(csrf, cameras, rules, selected, draft)
            content += f'<section><h3>Recent delivery activity</h3>{alarm_activity(recent, zone)}</section>'
        shell = admin_shell("alarms", content, csrf, message=message, error=status >= 400, actions=add)
        return HTMLResponse(page("Alarm rules", shell), status_code=status)

    @app.get("/admin/alarms", response_class=HTMLResponse)
    def alarms(request: Request, camera: str | None = None, saved: bool = False, view: str = "rules", smtp_saved: bool = False):
        csrf = admin_csrf(request)
        if not csrf:
            return RedirectResponse("/admin", status_code=303)
        return alarm_page(csrf, camera, view=view,
                          message="SMTP settings saved." if smtp_saved else "Alarm rule saved." if saved else "")

    @app.post("/admin/alarms")
    async def save_alarm(request: Request):
        csrf, fields = await authorized_form(request, limit=12_288)
        camera = fields.get("camera", "")
        try:
            rule = parse_rule(fields)
            await run_in_threadpool(store.save_alarm_rule, camera, rule)
        except ValueError as exc:
            if "@" in fields.get("command", ""):
                fields["command"] = ""
            if not fields.get("secret_name", "").strip().startswith(SECRET_PREFIX):
                fields["secret_name"] = ""
            return alarm_page(csrf, camera, draft=fields, message=str(exc), status=400)
        return RedirectResponse("/admin/alarms?" + urlencode({"camera": camera, "saved": "true"}) + "#alarm-editor",
                                status_code=303)

    @app.post("/admin/smtp")
    async def save_smtp(request: Request):
        csrf, fields = await authorized_form(request, limit=8192)
        try:
            config = parse_smtp_settings(fields)
        except ValueError as exc:
            draft = {**DEFAULT_SMTP, **{key: fields[key] for key in DEFAULT_SMTP if key in fields}}
            if not draft["secret_name"].startswith(SMTP_SECRET_PREFIX):
                draft["secret_name"] = ""
            return alarm_page(csrf, None, view="smtp", smtp_draft=draft, message=str(exc), status=400)
        if fields.get("test_connection") == "yes":
            result, detail = await run_in_threadpool(test_smtp_connection, config, data_dir=store.database.parent)
            record_event(store.database.parent, "smtp.test", "admin", outcome=result, security=config["security"],
                         level="info" if result == "accepted" else "warning")
            return alarm_page(csrf, None, view="smtp", smtp_draft=config, message=detail,
                              status=200 if result == "accepted" else 400)
        await run_in_threadpool(store.save_smtp_settings, config)
        return RedirectResponse("/admin/alarms?view=smtp&smtp_saved=true", status_code=303)

    @app.get("/admin/registered-vehicles/template.csv")
    def registration_template(request: Request):
        if not admin_csrf(request):
            raise HTTPException(403, "Sign in as administrator.")
        return Response("Plate\r\n", media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="platelog-registered-plates.csv"'})

    @app.get("/admin/import-review", response_class=HTMLResponse)
    def registration_review(request: Request, run: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
                            number: int = Query(1, alias="page", ge=1)):
        csrf = admin_csrf(request)
        if not csrf:
            return RedirectResponse("/admin", status_code=303)
        saved_zone = store.setting("display_timezone")
        zone = ZoneInfo(saved_zone) if saved_zone else display_zone
        try:
            content = review_content(store, run, number, zone)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        shell = admin_shell("registered-vehicles", content, csrf, heading="Skipped entries",
                            description="Review entries that could not be added to the registered list.",
                            actions='<a href="/admin?section=registered-vehicles">Back to list</a>')
        return HTMLResponse(page("Skipped entries", shell))

    @app.get("/admin/import-review/{run_id}/skipped.csv")
    def registration_review_csv(request: Request, run_id: str):
        if not admin_csrf(request):
            raise HTTPException(403, "Sign in as administrator.")
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise HTTPException(404, "Import not found.")
        try:
            content = skipped_csv(store, run_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(content, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="platelog-skipped-{run_id}.csv"'})

    @app.post("/admin/registered-vehicles/{action}")
    async def import_registrations(request: Request, action: str, mode: str, file_type: str = Query(alias="format"),
                                   filename: str = Query("Manual upload", max_length=255)):
        require_form_origin(request)
        csrf = admin_csrf(request)
        if not csrf:
            raise HTTPException(403, "Sign in as administrator.")
        if not hmac.compare_digest(csrf.encode(), request.headers.get("X-CSRF-Token", "").encode()):
            raise HTTPException(403, "Reload the Admin page before submitting.")
        if action not in {"preview", "apply"} or mode not in {"add", "replace"} or file_type not in {"csv", "xlsx"}:
            raise HTTPException(400, "Choose a CSV or .xlsx file and a valid import mode.")
        replacing = mode == "replace"
        applying = action == "apply"
        if applying and replacing and request.headers.get("X-Confirm-Replace") != "yes":
            raise HTTPException(400, "Confirm that plates missing from the file will be removed.")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > IMPORT_BYTES:
                raise HTTPException(413, "Plate lists can be up to 4 MiB. Save large workbooks as CSV.")
            body.extend(chunk)

        digest = hashlib.sha256(body).hexdigest()

        def signature(revision: str) -> str:
            message = f"{csrf}:{mode}:{file_type}:{digest}:{revision}".encode()
            return hmac.digest(import_secret, message, "sha256").hex()

        expected = None
        if applying:
            receipt = request.headers.get("X-Import-Receipt", "")
            match = re.fullmatch(r"([a-f0-9]{32}|)\.([a-f0-9]{64})", receipt)
            if not match or not hmac.compare_digest(signature(match[1]), match[2]):
                raise HTTPException(409, "Preview this file and import mode before applying.")
            expected = match[1]

        def process_import():
            source = filename.replace("\\", "/").rsplit("/", 1)[-1] or "Manual upload"
            try:
                imported = parse_plate_import(bytes(body), file_type)
            except ValueError as exc:
                store.record_registration_failure(source, mode, str(exc), getattr(exc, "imported", None))
                record_event(store.database.parent, "registration.import_failed", "registered_list", level="warning", error=exc)
                raise
            result = store.registration_import(imported.plates, replacing, expected, imported=imported, source=source)
            if applying:
                record_event(store.database.parent, "registration.import_completed", "registered_list",
                             added=result["added"], removed=result["removed"], total=result["total"], skipped=imported.skipped_entries)
            result["import_report"] = imported.report()
            revision = result.pop("revision")
            if not applying:
                result["receipt"] = f"{revision}.{signature(revision)}"
                result["duplicates"] = imported.duplicates
            return result

        try:
            return await run_in_threadpool(process_import)
        except RegistrationChanged as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/admin/register-access")
    async def save_register_access(request: Request):
        csrf, fields = await authorized_form(request)
        required = fields.get("required") == "yes"
        password = fields.get("password", "")
        try:
            verifier = await run_in_threadpool(password_verifier, password) if password else ""
            await run_in_threadpool(store.set_register_access, required, verifier)
        except ValueError as exc:
            return dashboard(csrf, str(exc), 400, section="access")
        return RedirectResponse("/admin?access_saved=true#register-access", status_code=303)

    @app.post("/admin/audible-alerts")
    async def save_audible_alerts(request: Request):
        csrf, fields = await authorized_form(request)
        if any(fields.get(key, "") not in {"", "yes"} for key in ("registered", "unregistered")):
            return dashboard(csrf, "Choose which vehicle sounds to enable.", 400, section="audible-alerts")
        settings = {key: fields.get(key) == "yes" for key in ("registered", "unregistered")}
        await run_in_threadpool(store.set_setting, "audible_alerts", json.dumps(settings), "Audible alert settings saved")
        return RedirectResponse("/admin?sounds_saved=true#audible-alerts", status_code=303)

    @app.post("/admin/settings")
    async def save_retention(request: Request):
        csrf, fields = await authorized_form(request)
        try:
            days = {name: int(fields[name]) for name in ("passage_days", "photo_days")}
            if not 1 <= days["photo_days"] <= days["passage_days"] <= 3650:
                raise ValueError
        except (KeyError, ValueError):
            return dashboard(csrf, "Enter 1–3650 days. Photo retention must be no longer than passage retention.", 400, section="retention")
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
        if fields.get("test_folder") == "yes":
            draft = {"enabled": enabled, "run_time": run_time, "folder": folder}
            try:
                await run_in_threadpool(check_report_folder, folder)
            except ValueError as error:
                record_event(store.database.parent, "admin.folder_test", "admin", level="warning", error=error, outcome="failed")
                return dashboard(csrf, status=400, csv_draft=draft, folder_result=str(error))
            record_event(store.database.parent, "admin.folder_test", "admin", outcome="success")
            return dashboard(csrf, csv_draft=draft,
                             folder_result=f"Folder test passed for {folder}. PlateLog wrote, read, and removed a test file.")
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", run_time):
            return dashboard(csrf, "Enter a run time between 00:00 and 23:59.", 400, section="csv-reports")
        if enabled or folder:
            try:
                folder = str(report_folder(folder))
            except ValueError as exc:
                return dashboard(csrf, str(exc), 400, section="csv-reports")
        schedule = json.dumps({"enabled": enabled, "run_time": run_time, "folder": folder})
        activity = "Daily CSV schedule enabled" if enabled else "Daily CSV schedule disabled"
        await run_in_threadpool(store.set_setting, "daily_csv_schedule", schedule, activity)
        return RedirectResponse("/admin?csv_saved=true#csv-reports", status_code=303)

    @app.post("/admin/registered-refresh")
    async def save_registered_refresh(request: Request):
        csrf, fields = await authorized_form(request, limit=16_384)
        enabled = fields.get("enabled") == "yes"
        source = fields.get("source", "").strip()
        run_time = fields.get("run_time", "")
        try:
            await run_in_threadpool(save_refresh_schedule, store, enabled, source, run_time)
        except ValueError as exc:
            draft = {"enabled": enabled, "source": source, "run_time": run_time}
            return dashboard(csrf, status=400, refresh_draft=draft, refresh_notice=str(exc))
        return RedirectResponse("/admin?refresh_saved=true#registered-refresh", status_code=303)

    @app.post("/admin/purge")
    async def purge(request: Request):
        csrf, fields = await authorized_form(request)
        if fields.get("confirm") != "yes":
            return dashboard(csrf, "Confirm permanent deletion first.", 400, section="retention")
        try:
            limits = int(fields.get("passage_days", "")), int(fields.get("photo_days", ""))
        except ValueError:
            return dashboard(csrf, "Review the saved age limits first.", 400, section="retention")
        try:
            passages, photos = await run_in_threadpool(store.apply_retention, confirmed_limits=limits)
        except ValueError as exc:
            return dashboard(csrf, str(exc), 409, section="retention")
        return dashboard(csrf, f"Purged {passages} passages and {photos} photos.", section="retention")

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
            return dashboard(csrf, "Enter a valid IANA time zone, such as Australia/Sydney.", 400, section="system")
        await run_in_threadpool(store.set_setting, "display_timezone", name, f"Display time zone changed to {name}")
        return dashboard(csrf, "Time zone saved.", section="system")

    @app.post("/admin/unregistered-history")
    async def save_unregistered_history(request: Request):
        csrf, fields = await authorized_form(request)
        try:
            value = fields.get("days", "")
            if not re.fullmatch(r"[0-9]{1,4}", value):
                raise ValueError("Enter a history window from 1 to 3650 days.")
            await run_in_threadpool(store.set_unregistered_history_days, int(value))
        except ValueError as exc:
            return dashboard(csrf, str(exc), 400, section="system")
        return dashboard(csrf, "Unregistered history window saved.", section="system")

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
        render = initial_password_page if current.startswith(INITIAL_ADMIN_PREFIX) else lambda token, message, code: dashboard(token, message, code, section="access")
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
            return dashboard(csrf, "Confirm the restart first.", 400, section="system")
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
            return dashboard(csrf, "Enter a TCP port from 1 to 65535.", 400, section="system")
        port = int(port_text)
        if port == current_port:
            return dashboard(csrf, "PlateLog is already using this port.", section="system")
        if port == other_port:
            return dashboard(csrf, "The staff web and camera POST ports must be different.", 409, section="system")
        destination = str(request.url.replace(port=port, path="/admin", query="")) if move_admin else "/admin"
        restarting = True
        try:
            await run_in_threadpool(change_port, port)
        except OSError:
            restarting = False
            return dashboard(csrf, "PlateLog could not open that port. Choose another port.", 409, section="system")
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
                 camera_online: bool = False, change_camera_port: Callable[[int], None] | None = None,
                 pilot_camera_transport: str | None = None) -> FastAPI:
    if allowed_host != "*" and not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", allowed_host):
        raise ValueError("Expected a hostname, IPv4 address, or '*' for --allowed-host")
    store = Store(data_dir)
    if persistent and store.setting("admin_password") is None:
        store.initialize_admin_password(initial_admin_verifier())

    async def supervise_retention():
        while True:
            try:
                await run_in_threadpool(store.apply_retention)
            except Exception as exc:
                record_event(data_dir, "retention.failed", "retention", level="error", error=exc)
                LOG.exception("PlateLog retention failed")
            await asyncio.sleep(3600)

    async def supervise_csv():
        while True:
            try:
                await run_in_threadpool(export_due_report, store, display_zone)
            except Exception as exc:
                record_event(data_dir, "csv.failed", "csv", level="error", error=exc)
                LOG.exception("PlateLog daily CSV export failed")
            await asyncio.sleep(30)

    async def supervise_registered_refresh():
        while True:
            try:
                await run_in_threadpool(refresh_due_registrations, store, display_zone)
            except Exception as exc:
                record_event(data_dir, "registration.refresh_failed", "registered_list", level="error", error=exc)
                LOG.exception("PlateLog scheduled registered list refresh failed")
            await asyncio.sleep(30)

    async def supervise_alarms():
        while True:
            try:
                await run_in_threadpool(run_due_alarms, store, display_zone=str(display_zone))
            except Exception as exc:
                record_event(data_dir, "alarm.failed", "alarms", level="error", error=exc)
                LOG.exception("PlateLog alarm processing failed")
            await asyncio.sleep(0.5)

    @asynccontextmanager
    async def lifespan(_app):
        record_event(data_dir, "service.ready", "service", staff_port=web_port, camera_port=camera_port,
                     camera_online=camera_online, mode="pilot" if pilot_camera_transport is not None else "preview")
        jobs = (supervise_retention, supervise_csv, supervise_registered_refresh, supervise_alarms) if persistent else ()
        tasks = [asyncio.create_task(job()) for job in jobs]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            record_event(data_dir, "service.stopped", "service")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", allowed_host], www_redirect=False)
    require_register, register_sign_out_form = admin_routes(
        app, store, persistent, allow_http_admin, restart, web_port, change_web_port, display_zone,
        camera_port, camera_online, change_camera_port, pilot_camera_transport,
    )
    signed_in = [Depends(require_register)]

    @app.middleware("http")
    async def preview_headers(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            await run_in_threadpool(record_event, data_dir, "request.failed", "web", level="error", error=exc,
                                    route=route_category(request.url.path), method=request.method)
            raise
        if response.status_code >= 400:
            await run_in_threadpool(record_event, data_dir, "request.rejected", "web",
                                    level="error" if response.status_code >= 500 else "warning",
                                    status=response.status_code, route=route_category(request.url.path), method=request.method)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'"
        return response

    @app.get("/healthz")
    def health():
        return {"status": "ready", "mode": "pilot" if pilot_camera_transport is not None else "synthetic-preview"}

    @app.get("/preview.css")
    def stylesheet():
        return Response(CSS + ADMIN_CSS, media_type="text/css")

    @app.get("/admin.js")
    def admin_script():
        return Response(ADMIN_JS, media_type="text/javascript")

    @app.get("/preview.js")
    def browser_script():
        return Response(JS, media_type="text/javascript")

    @app.get("/audible.js")
    def audible_script():
        return Response(AUDIBLE_JS, media_type="text/javascript")

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

    @app.get("/register/audible-alerts", dependencies=signed_in)
    def audible_events(since: int | None = Query(None, ge=0, le=253402300799999),
                       camera: str = Query("", max_length=128)):
        return store.audible_events(since, camera)

    @app.get("/register/stats", response_class=HTMLResponse, dependencies=signed_in)
    def register_stats(camera: str = Query("", max_length=128)):
        return render_stats(store, time.time_ns() // 1_000_000, camera, register_zone())

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
                          for key in ("history", *VEHICLE_COLUMNS))
        toolbar = f'<div class="toolbar">{controls}<fieldset><legend>Columns</legend>{toggles}</fieldset></div>'
        activity = render_stats(store, time.time_ns() // 1_000_000, filters.camera, filters.zone)
        stats = f'''<section class="register-stats" aria-labelledby="stats-heading">
            <h2 id="stats-heading">Vehicle activity · Last 24 hours</h2>
            <div id="register-stats-content">{activity}</div></section>'''
        return register_page(request, "Plate log", register_sign_out_form(request) + REGISTER_CONTROL + stats + toolbar +
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
        history_days = rows[0]["history_days"]
        history_help = help_tip("unregistered-history", "Previous unregistered passes",
            f"Shown only for currently unregistered vehicles. Counts earlier unregistered passages for this plate across all cameras in the {history_days} days ending at this row's read time, excluding this passage. Zero means no confirmed earlier unregistered passage in that window. A dash on a registered vehicle means history is hidden. Unknown means no reliable historical classification. Counts use retained passage records; later CSV imports do not rewrite the recorded status. Hide this column using the History checkbox.")
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
            registered = "true" if row["registered"] else "false"
            registration = "Registered" if row["registered"] else "Unregistered"
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
            count, unknown = row["unregistered_passes"], row["history_unknown"]
            recorded = "Unknown" if row["registered_at_read"] is None else "Registered" if row["registered_at_read"] else "Unregistered"
            if row["registered"]:
                history_cell = '<td class="passage-history muted" data-column="history" title="Currently registered; unregistered history is hidden.">—</td>'
            else:
                history_title = escape(f"{count} confirmed previous unregistered passages in {history_days} days before this read; this passage is excluded. "
                                       f"{unknown} earlier passages have unknown historical status.")
                history_cell = (f'<td class="passage-history" data-column="history" data-unregistered-count="{count}" data-history-unknown="{unknown}" title="{history_title}">'
                                f'<strong>{count if count or not unknown else "—"}</strong>'
                                f'{f"<span class=history-unknown>{unknown} unknown</span>" if unknown else ""}</td>')
            result.append(f'''<tr data-registered="{registered}"><td>{photo}</td>
                <td class="registration"><a href="{link}">{plate_text}</a><span class="registration-status" title="Current list status. At first receipt: {recorded}.">{registration}</span></td>
                {history_cell}<td>{date}</td><td><time datetime="{capture.isoformat()}" title="{capture.strftime('%Z %z')}">{time}</time></td>{cells}</tr>''')
        headings = "".join(f'<th scope="col" data-column="{key}">{key.title()}</th>' for key in VEHICLE_COLUMNS)
        return f'''<div data-register-page="{page}">{top}<p id="register-summary" class="muted">{caption}</p>
            <div class="scroll"><table aria-labelledby="register-summary">
            <thead><tr><th scope="col">Image</th><th scope="col">Reg</th>
            <th scope="col" class="history-heading" data-column="history"><span class="history-label">Previous unregistered<span class="history-window">passes · {history_days} days</span></span> {history_help}</th>
            <th scope="col">Date</th><th scope="col">Time</th>{headings}</tr></thead>
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
            links = f'<a href="{back}">Back to filtered plate log</a>'
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
        record_event(store.database.parent, "excel.completed", "web", passages=len(rows), bytes=len(report))
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
