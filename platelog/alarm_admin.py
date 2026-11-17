"""Render the Admin alarm rules and the latest delivery decisions."""

from datetime import datetime, timezone
from html import escape
from sqlite3 import Row
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .alarms import ALARM_MODES, ALARM_TRIGGERS, MAX_COMMAND_LENGTH, SECRET_PREFIX, pulse_path

RESULTS = {
    "registered": "Registered",
    "unregistered": "Unregistered",
    "held": "Held",
    "logged": "Would pulse",
    "dispatching": "Unconfirmed",
    "accepted": "Axis accepted",
    "failed": "Failed",
    "unconfirmed": "Unconfirmed",
}


def options(items: dict[str, str], selected: str) -> str:
    return "".join(f'<option value="{escape(key)}"{" selected" if selected == key else ""}>{escape(label)}</option>'
                   for key, label in items.items())


def rule_url(rule: Row) -> str:
    return f'https://{rule["module"]}{pulse_path(rule["virtual_input"], rule["pulse_seconds"])}' if rule["module"] else ""


def rule_editor(camera_cell: str, mode: str, trigger: str, command: str, username: str, secret_name: str) -> str:
    return f'''<tr id="alarm-editor" data-selected><td>{camera_cell}</td>
        <td><select name="trigger" aria-label="Trigger">{options(ALARM_TRIGGERS, trigger)}</select></td>
        <td><textarea name="command" aria-label="VAPIX command" maxlength="{MAX_COMMAND_LENGTH}" rows="3"
            placeholder="Paste the timed HTTPS VAPIX URL" spellcheck="false">{escape(command)}</textarea></td>
        <td><div class="alarm-credentials">
            <input name="username" aria-label="Axis username" maxlength="128" value="{escape(username)}"
                placeholder="Axis username" autocomplete="off">
            <input name="secret_name" aria-label="Password variable" maxlength="80" value="{escape(secret_name)}"
                placeholder="{SECRET_PREFIX}ENTRY" spellcheck="false" autocomplete="off">
        </div></td>
        <td><select name="mode" aria-label="Mode">{options(ALARM_MODES, mode)}</select></td>
        <td><div class="alarm-actions"><button type="submit">Save rule</button><a href="/admin/alarms">Cancel</a></div></td></tr>'''


def alarm_table(csrf: str, cameras: list[Row], rules: list[Row], selected: str | None,
                draft: dict[str, str] | None) -> str:
    saved = {rule["camera"]: rule for rule in rules}
    rows = []
    for rule in rules:
        camera = escape(rule["camera"])
        command = rule_url(rule)
        if selected == rule["camera"]:
            fields = draft or {}
            cell = f'<input type="hidden" name="camera" value="{camera}"><strong>{camera}</strong>'
            rows.append(rule_editor(cell, fields.get("mode", rule["mode"]), fields.get("trigger", rule["plate_status"]),
                                    fields.get("command", command), fields.get("username", rule["username"]),
                                    fields.get("secret_name", rule["secret_name"])))
        else:
            link = urlencode({"camera": rule["camera"]})
            rows.append(f'''<tr><th scope="row">{camera}</th><td>{ALARM_TRIGGERS[rule["plate_status"]]}</td>
                <td><code>{escape(command or "—")}</code></td>
                <td>{escape(rule["username"] or "—")}<br><code>{escape(rule["secret_name"])}</code></td>
                <td>{ALARM_MODES[rule["mode"]]}</td>
                <td><a href="/admin/alarms?{link}#alarm-editor" aria-label="Edit alarm for {camera}">Edit</a></td></tr>''')
    if selected not in saved and (selected is not None or not rules):
        available = {row["id"]: row["id"] for row in cameras if row["id"] not in saved}
        if available:
            choices = options({"": "Choose a camera", **available}, selected or "")
            cell = f'<select name="camera" aria-label="Camera" required>{choices}</select>'
            fields = draft or {}
            rows.append(rule_editor(cell, fields.get("mode", "log"), fields.get("trigger", "unregistered"),
                                    fields.get("command", ""), fields.get("username", ""), fields.get("secret_name", "")))
    if not rows:
        return '<p class="muted">Add a source camera in <a href="/admin#axis-cameras">Axis cameras</a>.</p>'
    return f'''<form class="alarm-rule-form" method="post" action="/admin/alarms">
        <input type="hidden" name="csrf" value="{csrf}">
        <div class="scroll camera-table" role="region" aria-label="Saved alarm rules" tabindex="0">
        <table aria-label="Alarm rules"><thead><tr><th scope="col">Camera</th>
        <th scope="col">Trigger</th><th scope="col">VAPIX command</th><th scope="col">Credentials</th><th scope="col">Mode</th>
        <th scope="col">Actions</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></form>'''


def alarm_activity(recent: list[Row], zone: timezone | ZoneInfo) -> str:
    if not recent:
        return '<p class="muted">No alarm decisions yet.</p>'
    rows = []
    for item in recent:
        recorded = datetime.fromtimestamp(item["result_ms"] / 1000, zone)
        plate = escape(item["plate"])
        rows.append(f'''<tr><td><time datetime="{recorded.isoformat()}">{recorded:%d/%m %H:%M:%S}</time></td>
            <td>{escape(item["camera"])}</td><td><a href="/passages/{item["passage_id"]}">{plate}</a></td>
            <td><strong>{RESULTS[item["result"]]}</strong><br>{escape(item["detail"])}</td></tr>''')
    return f'''<div class="scroll camera-table" role="region" aria-label="Recent alarm activity" tabindex="0">
        <table aria-label="Recent alarm activity"><thead><tr><th scope="col">Time</th><th scope="col">Camera</th>
        <th scope="col">Plate</th><th scope="col">Result</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'''
