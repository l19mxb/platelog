"""Send deterministic synthetic camera sequences to a loopback receiver."""

import base64
import hashlib
import json
from http.client import HTTPConnection
from io import BytesIO
from pathlib import Path
from time import time_ns
from urllib.parse import urlencode
from uuid import uuid4

from PIL import Image, ImageDraw

from .receiver import camera_authorization

FIXTURES = Path(__file__).with_name("fixtures")

def demo_jpeg(plate: str) -> bytes:
    image = Image.new("RGB", (480, 180), "#263748")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((78, 48, 402, 130), radius=8, fill="#f5f3e9")
    draw.text((240, 87), plate, fill="#17232f", anchor="mm", font_size=38)
    draw.text((240, 153), "SYNTHETIC TEST IMAGE", fill="white", anchor="mm", font_size=14)
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def payload(when: int, counter: int, plate: str, state: str, direction: str = "unknown") -> dict:
    return {
        "capture_timestamp": str(when), "packetCounter": str(counter), "plateText": plate,
        "carState": state, "carMoveDirection": direction, "carID": "42", "profileID": "1", "roiID": "1",
    }


def multipart(event: dict, photo: bytes) -> tuple[str, bytes]:
    boundary = uuid4().hex
    parts = [
        b'Content-Disposition: form-data; name="json"; filename="event.json"\r\n'
        b"Content-Type: application/json\r\n\r\n" + json.dumps(event).encode(),
        b'Content-Disposition: form-data; name="image"; filename="synthetic.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n" + photo,
    ]
    marker = b"--" + boundary.encode()
    body = b"".join(marker + b"\r\n" + part + b"\r\n" for part in parts) + marker + b"--\r\n"
    return f"multipart/form-data; boundary={boundary}", body


def call(port: int, path: str, authorization: str = "", body: bytes | None = None,
         content_type: str = "application/json") -> tuple[int, bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": authorization, "Content-Type": content_type}
    try:
        connection.request("GET" if body is None else "POST", path, body, headers)
        with connection.getresponse() as response:
            return response.status, response.read()
    finally:
        connection.close()


def expect(result: tuple[int, bytes], status: int = 200) -> bytes:
    received, body = result
    if received != status:
        raise RuntimeError(f"Expected HTTP {status}, received {received}")
    return body


def check(condition: bool, description: str) -> None:
    if not condition:
        raise RuntimeError(description)


def run_scenario(port: int, token: str) -> dict:
    now = time_ns() // 1_000_000
    reader = f"Bearer {token}"
    entry = camera_authorization("demo-entry", token)
    exit_camera = camera_authorization("demo-exit", token)
    entry_path = "/api/axis/demo-entry/events"
    exit_path = "/api/axis/demo-exit/events"

    def send(path: str, auth: str, event: dict) -> dict:
        body = expect(call(port, path, auth, json.dumps(event).encode()))
        return json.loads(body)

    jpeg = (FIXTURES / "entry.jpg").read_bytes()
    first = payload(now, 1, "DEMO-AO1", "new")
    first["vehicle_info"] = {"make": "Audi", "model": "A4", "color": "GREY"}
    first["ImageArray"] = [{"ImageFormat": "jpg", "BinaryImage": base64.b64encode(jpeg).decode("ascii")}]
    initial = send(entry_path, entry, first)
    replay = send(entry_path, entry, first)
    check(replay == {**initial, "duplicate": True} and not initial["duplicate"], "Replay changed the stored packet")

    # Axis can repeat the New timestamp on Lost. Deliver Lost before Update
    # to exercise a buffered correction arriving after the track has closed.
    closed = send(entry_path, entry, payload(now, 3, "DEMO-A01", "lost", "in"))
    correction = send(entry_path, entry, payload(now + 250, 2, "DEMO-A01", "update", "in"))
    check(initial["passage_id"] == closed["passage_id"] == correction["passage_id"], "Track split across multiple passages")

    second_jpeg = (FIXTURES / "exit.jpg").read_bytes()
    second_event = payload(now + 500, 1, "DEMO-B02", "update", "out")
    second_event["vehicle_info"] = {"make": "Toyota", "model": "Corolla", "color": "BLUE"}
    kind, body = multipart(second_event, second_jpeg)
    second = json.loads(expect(call(port, exit_path, exit_camera, body, kind)))
    check(second["passage_id"] != initial["passage_id"], "Two cameras were merged")

    passage_path = f"/api/passages/{initial['passage_id']}"
    passage = json.loads(expect(call(port, passage_path, reader)))
    check(passage["closed"] and passage["direction"] == "in" and passage["plate"] == "DEMO-A01" and
          passage["packets"] == 3 and passage["images"] == 1, "Unexpected corrected passage")
    source = next(packet for packet in passage["history"] if packet["id"] == initial["packet_id"])
    check(source["plate"] == "DEMO-AO1", "Original OCR text was lost")
    image = source["images"][0]
    photo_path = f"/api/images/{image['id']}"
    expect(call(port, photo_path), 401)
    expect(call(port, photo_path, entry), 401)
    expect(call(port, exit_path, entry, b"{}"), 401)
    saved = expect(call(port, photo_path, reader))
    check(saved == jpeg and image["sha256"] == hashlib.sha256(jpeg).hexdigest(), "Stored photo differs from the original")
    check(passage["photo_id"] == image["id"] and (passage["make"], passage["model"], passage["colour"]) == ("Audi", "A4", "Grey"),
          "The register lost its thumbnail or vehicle details after correction")
    expect(call(port, entry_path, entry, b"{}"), 400)

    query = urlencode({"plate": "demo-ao1", "camera": "demo-entry"})
    rows = json.loads(expect(call(port, f"/api/passages?{query}", reader)))
    check(any(row["id"] == initial["passage_id"] for row in rows), "Search missed the earlier OCR text")
    second_passage = json.loads(expect(call(port, f"/api/passages/{second['passage_id']}", reader)))
    check(second_passage["images"] == 1 and second_passage["direction"] == "out", "Multipart evidence was not saved")
    second_image = second_passage["history"][0]["images"][0]
    second_saved = expect(call(port, f"/api/images/{second_image['id']}", reader))
    check(second_saved == second_jpeg, "Multipart photo differs from the original")

    return {"entry": {key: value for key, value in passage.items() if key != "history"},
            "exit": {key: value for key, value in second_passage.items() if key != "history"},
            "image_sha256": image["sha256"], "replay_ignored": True,
            "unauthenticated_image_status": 401, "invalid_payload_status": 400}
