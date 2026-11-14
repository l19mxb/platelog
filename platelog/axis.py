"""Decode the documented Axis push formats without fetching camera URLs."""

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from io import BytesIO

from PIL import Image

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGES = 3
MAX_BODY_BYTES = 13 * 1024 * 1024
MAX_PIXELS = 12_000_000


class PayloadError(ValueError):
    """The request does not match a supported event format."""


@dataclass(frozen=True)
class Event:
    capture_ms: int
    plate: str
    direction: str
    state: str
    runtime_id: str
    profile: str
    area: str
    counter: int | None
    metadata: str
    images: tuple[bytes, ...]
    fingerprint: str


def plate_key(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def split_multipart(content_type: str, body: bytes) -> tuple[bytes, list[bytes]]:
    if not content_type.isascii() or "\r" in content_type or "\n" in content_type:
        raise PayloadError("Invalid multipart content type")
    try:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body
        )
    except (ValueError, RecursionError) as exc:
        raise PayloadError("Invalid multipart body") from exc
    if message.defects or not message.is_multipart():
        raise PayloadError("Invalid multipart body")
    metadata = None
    images = []
    for part in message.iter_parts():
        if part.defects or part.is_multipart() or part.get("Content-Transfer-Encoding", "binary").lower() not in {"binary", "8bit"}:
            raise PayloadError("Invalid multipart part")
        if part.get_content_disposition() != "form-data":
            raise PayloadError("Expected a form-data part")
        filename = (part.get_filename() or "").lower()
        name = part.get_param("name", header="content-disposition")
        kind = part.get_content_type()
        data = part.get_payload(decode=True)
        if kind == "application/json" or filename.endswith(".json") or (kind == "text/plain" and name == "json"):
            if metadata is not None:
                raise PayloadError("Expected one JSON part")
            metadata = data
        elif kind == "image/jpeg" or filename.endswith((".jpeg", ".jpg")):
            images.append(data)
            if len(images) > MAX_IMAGES:
                raise PayloadError("Too many images")
        else:
            raise PayloadError("Unsupported multipart part")
    if metadata is None:
        raise PayloadError("Missing JSON part")
    return metadata, images


def decode_inline(items: object) -> list[bytes]:
    if not isinstance(items, list) or len(items) > MAX_IMAGES:
        raise PayloadError("Invalid image array")
    result = []
    for item in items:
        value = item.get("BinaryImage") if isinstance(item, dict) else None
        if not isinstance(value, str) or len(value) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise PayloadError("Invalid inline image")
        try:
            result.append(base64.b64decode(value, validate=True))
        except (ValueError, binascii.Error) as exc:
            raise PayloadError("Invalid inline image") from exc
    return result


def validate_image(data: bytes) -> None:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise PayloadError("Invalid image size")
    try:
        with Image.open(BytesIO(data), formats=["JPEG"]) as image:
            if image.width * image.height > MAX_PIXELS:
                raise PayloadError("JPEG dimensions exceed the limit")
            image.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise PayloadError("Invalid JPEG") from exc


def parse_event(content_type: str, body: bytes) -> Event:
    images = []
    if media_type(content_type) == "multipart/form-data":
        body, images = split_multipart(content_type, body)
    try:
        raw = json.loads(body)
        if not isinstance(raw, dict):
            raise PayloadError("Expected a JSON object")
        inline_keys = [key for key in raw if key.lower() == "imagearray"]
        if len(inline_keys) > 1 or (inline_keys and images):
            raise PayloadError("Multiple image sources")
        if inline_keys:
            images = decode_inline(raw.pop(inline_keys[0]))
        metadata = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except PayloadError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PayloadError("Invalid event JSON") from exc

    capture = raw.get("capture_timestamp")
    if type(capture) not in {str, int} or not re.fullmatch(r"[0-9]{1,15}", str(capture)) or int(capture) > 253402300799999:
        raise PayloadError("Expected capture_timestamp in UTC milliseconds")

    def field(key: str, default: str = "") -> str:
        value = raw.get(key, default)
        if type(value) not in {str, int} or len(str(value)) > 128:
            raise PayloadError(f"Invalid {key}")
        text = str(value).strip()
        if any(unicodedata.category(char) in {"Cc", "Cs"} for char in text):
            raise PayloadError(f"Invalid {key}")
        return text

    plate = field("plateUTF8") or field("plateText") or field("plateASCII")
    if not plate_key(plate):
        raise PayloadError("Missing plate text")
    state = field("carState").lower()
    if state not in {"new", "update", "lost"}:
        raise PayloadError("Unsupported carState")
    direction = field("carMoveDirection").lower()
    if state == "new" or direction not in {"in", "out"}:
        direction = "unknown"
    counter = field("packetCounter")
    hashes = []
    for data in images:
        validate_image(data)
        hashes.append(hashlib.sha256(data).hexdigest())
    fingerprint = hashlib.sha256("\n".join([metadata, *hashes]).encode("ascii")).hexdigest()
    return Event(int(capture), plate, direction, state, field("carID"), field("profileID"),
                 field("roiID"), int(counter) if re.fullmatch(r"[0-9]{1,18}", counter) else None,
                 metadata, tuple(images), fingerprint)
