"""Bounded SMTP notifications; credentials remain in the service environment."""

import os
import re
import smtplib
import ssl
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from zoneinfo import ZoneInfo

SMTP_SECRET_PREFIX = "PLATELOG_SMTP_PASSWORD_"
SMTP_TIMEOUT = 5
MAX_RECIPIENTS = 10
DEFAULT_SUBJECT = "[PlateLog] {status}: {plate}"
DEFAULT_SMTP = {"host": "", "port": 587, "security": "starttls", "username": "", "secret_name": "", "sender": ""}
SECURITY_LABELS = {"starttls": "STARTTLS", "tls": "TLS (implicit)", "none": "None (internal relay)"}
_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
_HOST = rf"{_LABEL}(?:\.{_LABEL})*"
_MAILBOX = re.compile(rf"[a-zA-Z0-9!#$%&'*+/=?^_`{{|}}~.-]+@{_HOST}")


def mailbox(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Email addresses cannot contain control characters.")
    value = value.strip()
    if (len(value) > 254 or not _MAILBOX.fullmatch(value) or len(value.split("@")[0]) > 64
            or value.startswith(".") or ".@" in value or ".." in value):
        raise ValueError("Enter an email address without a display name, such as gatehouse@example.internal.")
    return value


def recipients(value: str) -> str:
    if not value.strip():
        return ""
    if len(value) > 2560:
        raise ValueError(f"Use no more than {MAX_RECIPIENTS} email recipients, separated by commas.")
    addresses = [mailbox(part) for part in value.split(",")]
    addresses = list(dict.fromkeys(addresses))
    if len(addresses) > MAX_RECIPIENTS:
        raise ValueError(f"Use no more than {MAX_RECIPIENTS} email recipients, separated by commas.")
    return ", ".join(addresses)


def subject_template(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("The email subject cannot contain line breaks or control characters.")
    value = value.strip() or DEFAULT_SUBJECT
    remaining = value
    for token in ("{plate}", "{camera}", "{status}"):
        remaining = remaining.replace(token, "")
    if (len(value) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "{" in remaining or "}" in remaining):
        raise ValueError("Use a subject up to 200 characters. Available fields: {plate}, {camera} and {status}.")
    return value


def parse_smtp_settings(fields: dict) -> dict:
    host = fields.get("host", "").strip().lower()
    if len(host) > 253 or not re.fullmatch(_HOST, host):
        raise ValueError("Enter the SMTP server hostname or IPv4 address without a URL or port.")
    port = str(fields.get("port", ""))
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ValueError("Enter an SMTP TCP port from 1 to 65535.")
    security = fields.get("security", "")
    if security not in SECURITY_LABELS:
        raise ValueError("Choose STARTTLS, TLS (implicit), or an internal relay without TLS.")
    username = fields.get("username", "").strip()
    secret_name = fields.get("secret_name", "").strip()
    if len(username) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in username):
        raise ValueError("Use an SMTP username up to 128 characters without control characters.")
    if secret_name and not re.fullmatch(rf"{SMTP_SECRET_PREFIX}[A-Z0-9_]{{1,48}}", secret_name):
        raise ValueError(f"Use a password variable beginning with {SMTP_SECRET_PREFIX}.")
    if bool(username) != bool(secret_name):
        raise ValueError("Supply both the SMTP username and password variable, or leave both blank for an IP-authorised relay.")
    if security == "none" and username:
        raise ValueError("SMTP authentication requires STARTTLS or TLS. Leave credentials blank for an internal relay.")
    return {"host": host, "port": int(port), "security": security, "username": username,
            "secret_name": secret_name, "sender": mailbox(fields.get("sender", ""))}


def smtp_ready(config: dict) -> bool:
    return bool(config.get("host") and config.get("sender")
                and (not config.get("username") or os.environ.get(config.get("secret_name", ""))))


@contextmanager
def smtp_connection(config: dict):
    secret = os.environ.get(config["secret_name"]) if config["username"] else ""
    if config["username"] and not secret:
        raise ValueError("The PlateLog service cannot read the SMTP password variable.")
    kwargs = {"timeout": SMTP_TIMEOUT, "local_hostname": "platelog"}
    if config["security"] == "tls":
        client = smtplib.SMTP_SSL(config["host"], config["port"], context=ssl.create_default_context(), **kwargs)
    else:
        client = smtplib.SMTP(config["host"], config["port"], **kwargs)
    try:
        client.ehlo_or_helo_if_needed()
        if config["security"] == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if config["username"]:
            client.login(config["username"], secret)
        yield client
    finally:
        client.close()


def smtp_error(error: Exception, *, sending: bool) -> tuple[str, str]:
    if isinstance(error, ssl.SSLCertVerificationError):
        return "failed", "The SMTP server's TLS certificate could not be verified."
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "failed", "The SMTP server rejected the credentials."
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return "failed", "The SMTP server does not support the selected TLS or authentication settings."
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "failed", "The SMTP server rejected all recipients."
    if isinstance(error, smtplib.SMTPResponseException):
        return "failed", f"The SMTP server rejected the request (code {error.smtp_code})."
    if isinstance(error, ValueError):
        return "failed", "Check the SMTP settings and service password variable."
    if sending:
        return "unconfirmed", "The SMTP server did not confirm acceptance. No automatic retry."
    return "failed", "Could not connect to the SMTP server."


def test_smtp_connection(config: dict) -> tuple[str, str]:
    """Connect, negotiate TLS/auth and NOOP. Does not send an email."""
    try:
        with smtp_connection(config) as client:
            code, _ = client.noop()
            if code != 250:
                return "failed", f"The SMTP server rejected the connection test (code {code})."
    except (OSError, ValueError) as error:
        return smtp_error(error, sending=False)
    return "accepted", "SMTP connection passed. TLS and authentication were checked where configured. No email was sent."


def send_alarm_email(config: dict, run: dict, zone_name: str) -> tuple[str, str]:
    message = EmailMessage()
    values = {"plate": " ".join(run["plate"].split()), "camera": run["camera"],
              "status": run["plate_status"].capitalize()}
    subject = run["email_subject"]
    for key, value in values.items():
        subject = subject.replace("{" + key + "}", value)
    message["From"] = config["sender"]
    addresses = run["email_recipients"].split(", ")
    message["To"] = ", ".join(addresses)
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid(domain="platelog.local")
    zone = ZoneInfo(zone_name)
    captured = datetime.fromtimestamp(run["capture_ms"] / 1000, zone).isoformat(timespec="seconds")
    received = datetime.fromtimestamp(run["received_ms"] / 1000, zone).isoformat(timespec="seconds")
    message.set_content(f"PlateLog vehicle alert\n\nPlate: {values['plate']}\nCamera: {values['camera']}\n"
                        f"Status: {values['status']}\nCaptured: {captured}\nReceived: {received}\n"
                        f"Time zone: {zone_name}\nPassage: {run['passage_id']}\n")
    sending = False
    try:
        with smtp_connection(config) as client:
            sending = True
            refused = client.send_message(message, from_addr=config["sender"], to_addrs=addresses)
            if refused:
                return "partial", f"SMTP accepted the email for {len(addresses) - len(refused)} of {len(addresses)} recipients. No automatic retry."
    except (OSError, ValueError) as error:
        return smtp_error(error, sending=sending)
    return "accepted", f"SMTP server accepted the email for {len(addresses)} recipient(s). Inbox delivery is not confirmed."
