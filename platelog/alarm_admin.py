"""Admin forms for per-camera alarm actions and shared SMTP delivery."""

import json
from datetime import datetime, timezone
from html import escape
from sqlite3 import Row
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .admin_ui import field
from .alarms import ALARM_ACTIONS, ALARM_MODES, ALARM_TRIGGERS, MAX_COMMAND_LENGTH, SECRET_PREFIX, pulse_path
from .email_alerts import DEFAULT_SUBJECT, SECURITY_LABELS, SMTP_SECRET_PREFIX

RESULTS = {"registered": "Registered", "unregistered": "Unregistered", "held": "Held", "logged": "Would send",
           "dispatching": "Unconfirmed", "accepted": "Accepted", "failed": "Failed", "unconfirmed": "Unconfirmed", "partial": "Partial"}


def options(items: dict[str, str], selected: str) -> str:
    return "".join(f'<option value="{escape(key)}"{" selected" if selected == key else ""}>{escape(label)}</option>'
                   for key, label in items.items())


def rule_url(rule: Row) -> str:
    return f'https://{rule["module"]}{pulse_path(rule["virtual_input"], rule["pulse_seconds"])}' if rule["module"] else ""


def rule_editor(csrf: str, camera_control: str, fields: dict, *, editing: bool) -> str:
    mode = field("rule-mode", "Mode", f'<select name="mode">{options(ALARM_MODES, fields.get("mode", "log"))}</select>',
                 "Off disables this rule. Log only records a match without sending a command or email. Use Live after testing.")
    trigger = field("rule-trigger", "Trigger", f'<select name="trigger">{options(ALARM_TRIGGERS, fields.get("trigger", "unregistered"))}</select>',
                    "Matches the latest plate against Registered vehicles after a two-second settling period. An empty list holds all actions.")
    action = field("rule-action", "Action", f'<select name="action">{options(ALARM_ACTIONS, fields.get("action", "vapix"))}</select>',
                   "Choose a timed VAPIX command, an email, or both. Each enabled action is attempted once per qualifying passage.")
    command = field("rule-command", "VAPIX command URL", f'<textarea name="command" rows="2" maxlength="{MAX_COMMAND_LENGTH}" '
                    f'placeholder="Paste the timed HTTPS VAPIX URL" spellcheck="false">{escape(fields.get("command", ""))}</textarea>',
                    "Paste an HTTPS /axis-cgi/virtualinput/activate.cgi URL with schemaversion=1, port=1–64 and duration=1–30 seconds. The certificate is verified.", wide=True)
    username = field("rule-username", "Username", f'<input name="username" maxlength="128" value="{escape(fields.get("username", ""))}" autocomplete="off">',
                     "The destination Axis device's operator username. This can differ from the source camera's POST credentials.")
    secret = field("rule-secret", "Password variable", f'<input name="secret_name" maxlength="80" value="{escape(fields.get("secret_name", ""))}" '
                   f'placeholder="{SECRET_PREFIX}ENTRY" spellcheck="false" autocomplete="off">',
                   f"Enter the environment variable name, beginning with {SECRET_PREFIX}. IT supplies its value to the PlateLog service. Do not paste the password here.")
    recipients = field("rule-recipients", "Recipients", f'<input name="email_recipients" maxlength="2560" '
                       f'value="{escape(fields.get("email_recipients", ""))}" placeholder="gatehouse@example.internal" spellcheck="false">',
                       "Up to 10 email addresses separated by commas. Use addresses only, without display names.")
    subject = field("rule-subject", "Subject", f'<input name="email_subject" maxlength="200" '
                    f'value="{escape(fields.get("email_subject", DEFAULT_SUBJECT))}">',
                    "Use {plate}, {camera} and {status} to insert the vehicle details. All other text is sent as written.")
    return f'''<section id="alarm-editor" class="rule-editor"><h3>{"Edit rule" if editing else "Add rule"}</h3>
        <form class="alarm-rule-form" data-rule-editor method="post" action="/admin/alarms">
        <input type="hidden" name="csrf" value="{escape(csrf)}"><div class="form-grid">{camera_control}{trigger}{mode}{action}</div>
        <fieldset class="alarm-action-fields" data-alarm-action="vapix"><legend>VAPIX action</legend>{command}{username}{secret}</fieldset>
        <fieldset class="alarm-action-fields" data-alarm-action="smtp"><legend>Email action</legend>{recipients}{subject}
        <p class="form-note">Includes plate, camera and event time. No photo attachment.
        <a href="/admin/alarms?view=smtp">SMTP settings</a> apply to every email rule.</p></fieldset>
        <div class="form-actions"><button type="submit">Save rule</button><a href="/admin/alarms">Cancel</a></div></form></section>'''


def alarm_table(csrf: str, cameras: list[Row], rules: list[Row], selected: str | None,
                draft: dict[str, str] | None) -> str:
    saved = {rule["camera"]: rule for rule in rules}
    rows = []
    for rule in rules:
        camera = escape(rule["camera"])
        link = urlencode({"camera": rule["camera"]})
        active = " data-selected" if selected == rule["camera"] else ""
        summary = []
        if rule["action"] != "smtp" and rule["module"]:
            summary.append(escape(rule["module"]))
        if rule["action"] != "vapix":
            summary.append(escape(rule["email_recipients"] or "No recipients"))
        rows.append(f'''<tr{active}><th scope="row">{camera}</th><td>{ALARM_TRIGGERS[rule["plate_status"]]}</td>
            <td>{ALARM_ACTIONS[rule["action"]]}<span class="rule-summary">{" · ".join(summary)}</span></td>
            <td>{ALARM_MODES[rule["mode"]]}</td>
            <td><a href="/admin/alarms?{link}#alarm-editor" aria-label="Edit alarm for {camera}">Edit</a></td></tr>''')
    table = ('<div class="scroll camera-table" role="region" aria-label="Saved alarm rules" tabindex="0">'
             '<table aria-label="Alarm rules"><thead><tr><th scope="col">Camera</th><th scope="col">Trigger</th>'
             '<th scope="col">Actions</th><th scope="col">Mode</th><th scope="col">Edit</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>') if rows else '<p class="muted">No alarm rules yet.</p>'
    if selected in saved:
        rule = saved[selected]
        fields = {**dict(rule), "trigger": rule["plate_status"], "command": rule_url(rule)}
        if draft is not None:
            fields = draft
        camera_control = field("rule-camera", "Camera", f'<input value="{escape(selected)}" readonly>',
                               "The source camera that supplies the vehicle read. Create a separate rule for each source camera.")
        camera_control += f'<input type="hidden" name="camera" value="{escape(selected)}">'
        return table + rule_editor(csrf, camera_control, fields, editing=True)
    if selected is not None or not rules:
        available = {row["id"]: row["id"] for row in cameras if row["id"] not in saved}
        if available:
            choices = options({"": "Choose a camera", **available}, selected or "")
            camera_control = field("rule-camera", "Camera", f'<select name="camera" required>{choices}</select>',
                                   "Select the source camera. One rule per camera can deliver VAPIX, email, or both.")
            return table + rule_editor(csrf, camera_control, draft or {}, editing=False)
    if not cameras:
        table += '<p>Add a source camera in <a href="/admin?section=axis-cameras">Axis cameras</a>.</p>'
    return table


def smtp_form(csrf: str, config: dict, *, message: str = "", error: bool = False) -> str:
    host = field("smtp-host", "SMTP server", f'<input name="host" maxlength="253" value="{escape(config["host"])}" '
                 'placeholder="mail.example.internal" spellcheck="false" required>',
                 "The mail relay hostname or IPv4 address reachable from the PlateLog server. Do not include a URL or port.")
    port = field("smtp-port", "Port", f'<input name="port" type="number" min="1" max="65535" value="{escape(str(config["port"]))}" required>',
                 "Use the port supplied by IT: commonly 587 for STARTTLS, 465 for implicit TLS, or 25 for an internal relay.")
    security = field("smtp-security", "Security", f'<select name="security">{options(SECURITY_LABELS, config["security"])}</select>',
                     "STARTTLS upgrades the connection to TLS. TLS (implicit) encrypts from connection start. The server certificate is verified. None is for an internal IP-authorised relay and cannot use a password.")
    sender = field("smtp-sender", "Sender email", f'<input name="sender" type="email" maxlength="254" value="{escape(config["sender"])}" '
                   'placeholder="platelog@example.internal" required>',
                   "The From address approved by IT for the relay. Recipients are set in each alarm rule.")
    username = field("smtp-user", "Username (optional)", f'<input name="username" maxlength="128" value="{escape(config["username"])}" autocomplete="off">',
                     "Leave both credential fields blank if IT authorises the PlateLog server by IP address.")
    secret = field("smtp-secret", "Password variable", f'<input name="secret_name" maxlength="80" value="{escape(config["secret_name"])}" '
                   f'placeholder="{SMTP_SECRET_PREFIX}RELAY" spellcheck="false" autocomplete="off">',
                   f"Enter the variable name, beginning with {SMTP_SECRET_PREFIX}. IT sets its value in the service environment. The password is not stored in the database.")
    notice = (f'<p class="notice{" error" if error else ""}" role="{"alert" if error else "status"}">{escape(message)}</p>'
              if message else "")
    return f'''<section id="smtp-settings"><h3>SMTP server</h3><p>One server for all email rules.</p>{notice}
        <form class="form-grid" method="post" action="/admin/smtp"><input type="hidden" name="csrf" value="{escape(csrf)}">
        {host}{port}{security}{sender}{username}{secret}
        <p class="form-note">Leave credentials blank for an IP-authorised internal relay. Password authentication requires TLS.</p>
        <p class="form-note smtp-warning" data-smtp-plain-warning{' hidden' if config['security'] != 'none' else ''}>None sends SMTP traffic without encryption. Use only the internal relay approved by IT.</p>
        <div class="form-actions"><button type="submit">Save SMTP settings</button>
        <button class="secondary" name="test_connection" value="yes" type="submit">Test connection</button></div></form>
        <p class="muted">The connection test checks the settings above without saving them or sending email.</p>
        <p>Set recipients and the subject in each alarm rule.</p></section>
        <section><h3>How email alerts work</h3><ul><li>Log only records matches without sending.</li>
        <li>Live sends one email per qualifying passage.</li><li>Server acceptance does not confirm inbox delivery.</li>
        <li>Unconfirmed or partially accepted messages are not retried automatically.</li></ul></section>'''


def alarm_activity(recent: list[Row], zone: timezone | ZoneInfo) -> str:
    if not recent:
        return '<p class="muted">No alarm decisions yet.</p>'
    rows = []
    for item in recent:
        recorded = datetime.fromtimestamp(item["result_ms"] / 1000, zone)
        details = []
        for action, delivery in json.loads(item["deliveries"]).items():
            details.append(f'<div><strong>{"VAPIX" if action == "vapix" else "Email"}: {RESULTS.get(delivery["result"], delivery["result"])}</strong>'
                           f' · {escape(delivery["detail"])}</div>')
        detail = "".join(details) if details else escape(item["detail"])
        rows.append(f'''<tr><td><time datetime="{recorded.isoformat()}">{recorded:%d/%m %H:%M:%S}</time></td>
            <td>{escape(item["camera"])}</td><td><a href="/passages/{item["passage_id"]}">{escape(item["plate"])}</a></td>
            <td><strong>{RESULTS[item["result"]]}</strong><br>{detail}</td></tr>''')
    return f'''<div class="scroll camera-table" role="region" aria-label="Recent alarm activity" tabindex="0">
        <table aria-label="Recent alarm activity"><thead><tr><th scope="col">Time</th><th scope="col">Camera</th>
        <th scope="col">Plate</th><th scope="col">Result</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'''
