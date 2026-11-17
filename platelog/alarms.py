"""Evaluate saved alarm candidates and send a timed Axis virtual-input request."""

import base64
import http.client
import os
import re
import ssl
import time
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree import ElementTree

if TYPE_CHECKING:
    from .store import Store

ALARM_MODES = {"off": "Off", "log": "Log only", "live": "Live"}
ALARM_TRIGGERS = {"registered": "Registered", "unregistered": "Unregistered"}
ACTIVATE_PATH = "/axis-cgi/virtualinput/activate.cgi"
SETTLE_MS = 2_000
MAX_LIVE_AGE_MS = 60_000
MAX_LIVE_FUTURE_MS = 5_000
MAX_PULSE_SECONDS = 30
MAX_COMMAND_LENGTH = 768
SECRET_PREFIX = "PLATELOG_AXIS_RELAY_PASSWORD_"
VIRTUAL_INPUT_NAMESPACE = "{http://www.axis.com/vapix/http_cgi/virtualinput1}"
AXIS_TIMEOUT = 3
MAX_REPLY_BYTES = 16_384


@dataclass(frozen=True)
class AlarmRule:
    mode: str
    trigger: str
    module: str
    virtual_input: int
    pulse_seconds: int
    username: str
    secret_name: str


def module_address(value: str) -> str:
    host, separator, port = value.strip().lower().removeprefix("https://").partition(":")
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(host) > 253 or not re.fullmatch(rf"{label}(?:\.{label})*", host):
        raise ValueError("Use an Axis hostname or IPv4 address in the command, with an optional HTTPS port.")
    if separator:
        if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
            raise ValueError("Use a valid HTTPS port in the command.")
        if int(port) != 443:
            return f"{host}:{int(port)}"
    return host


def parse_rule(fields: dict[str, str]) -> AlarmRule:
    mode = fields.get("mode", "")
    trigger = fields.get("trigger", "")
    if mode not in ALARM_MODES:
        raise ValueError("Choose Off, Log only or Live.")
    if trigger not in ALARM_TRIGGERS:
        raise ValueError("Choose Registered or Unregistered.")
    username = fields.get("username", "").strip()
    secret_name = fields.get("secret_name", "").strip()
    if username and not re.fullmatch(r"[!-9;-~]{1,128}", username):
        raise ValueError("Enter an Axis operator username without spaces or colons.")
    if secret_name and not re.fullmatch(rf"{SECRET_PREFIX}[A-Z0-9_]{{1,48}}", secret_name):
        raise ValueError(f"Use a password environment variable beginning with {SECRET_PREFIX}.")
    command = fields.get("command", "").strip()
    if not command and mode != "live":
        return AlarmRule(mode, trigger, "", 1, 1, username, secret_name)
    if (not command or len(command) > MAX_COMMAND_LENGTH or not command.isascii()
            or any(char.isspace() or ord(char) < 32 for char in command)):
        raise ValueError("Paste the full HTTPS VAPIX URL for a timed virtual input.")
    try:
        parts = urlsplit(command)
        query = parse_qs(parts.query, strict_parsing=True, max_num_fields=3)
    except ValueError as exc:
        raise ValueError("Paste the full HTTPS VAPIX URL for a timed virtual input.") from exc
    if parts.scheme != "https" or parts.path != ACTIVATE_PATH or "#" in command or "@" in parts.netloc or not parts.netloc:
        raise ValueError("Use the HTTPS virtual-input activation URL without credentials or a fragment.")
    if set(query) != {"schemaversion", "port", "duration"} or query["schemaversion"] != ["1"]:
        raise ValueError("Include schemaversion=1, port and duration in the VAPIX URL.")

    def number(key: str, maximum: int, message: str) -> int:
        values = query[key]
        if len(values) != 1 or not re.fullmatch(r"[0-9]{1,2}", values[0]) or not 1 <= int(values[0]) <= maximum:
            raise ValueError(message)
        return int(values[0])

    virtual_input = number("port", 64, "Enter a VAPIX port from 1 to 64.")
    pulse_seconds = number("duration", MAX_PULSE_SECONDS, f"Use a VAPIX duration from 1 to {MAX_PULSE_SECONDS} seconds.")
    return AlarmRule(mode, trigger, module_address(parts.netloc), virtual_input, pulse_seconds, username, secret_name)


def pulse_path(virtual_input: int, seconds: int) -> str:
    query = urlencode({"schemaversion": 1, "port": virtual_input, "duration": seconds})
    return f"{ACTIVATE_PATH}?{query}"


def pulse_axis(module: str, virtual_input: int, seconds: int, username: str, secret_name: str) -> tuple[str, str]:
    password = os.environ.get(secret_name)
    if not password:
        return "failed", "The PlateLog service cannot read the configured password variable."
    basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    with closing(http.client.HTTPSConnection(module, timeout=AXIS_TIMEOUT, context=ssl.create_default_context())) as axis:
        try:
            axis.connect()
        except ssl.SSLCertVerificationError:
            return "failed", "The Axis device's HTTPS certificate could not be verified."
        except OSError:
            return "failed", "Could not connect to the Axis device over HTTPS."
        try:
            axis.request("GET", pulse_path(virtual_input, seconds),
                         headers={"Authorization": f"Basic {basic}", "Accept": "text/xml", "Cache-Control": "no-store"})
            with axis.getresponse() as response:
                if response.status in {401, 403}:
                    return "failed", "Axis rejected the operator credentials or permissions."
                if response.status != 200:
                    return "unconfirmed", f"Axis returned HTTP {response.status}."
                body = response.read(MAX_REPLY_BYTES + 1)
        except (OSError, http.client.HTTPException):
            return "unconfirmed", "Axis did not confirm the request."

    if len(body) <= MAX_REPLY_BYTES and b"<!DOCTYPE" not in body:
        try:
            reply = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            pass
        else:
            ns = VIRTUAL_INPUT_NAMESPACE
            if reply.tag == f"{ns}VirtualInputResponse":
                changed = reply.findtext(f"{ns}Success/{ns}ActivateSuccess/{ns}StateChanged")
                if changed == "true":
                    return "accepted", "Axis confirmed activation of the virtual input."
                if changed == "false":
                    return "accepted", "Axis accepted the request and the input was already active."
                error = reply.findtext(f"{ns}Error/{ns}GeneralError/{ns}ErrorCode", "").strip()
                if re.fullmatch(r"[0-9]{1,5}", error):
                    return "failed", f"Axis rejected the activation (error {error})."
    return "unconfirmed", "Axis returned an unexpected activation response."


def run_due_alarms(store: "Store", now_ms: int | None = None) -> None:
    for _ in range(16):
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        claimed = store.claim_alarm(now)
        if claimed is None:
            return
        if claimed["result"] == "dispatching":
            result, detail = pulse_axis(claimed["module"], claimed["virtual_input"], claimed["pulse_seconds"],
                                        claimed["username"], claimed["secret_name"])
            store.finish_alarm(claimed["passage_id"], result, detail)
