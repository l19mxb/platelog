"""Local HTTP receiver for the synthetic PlateLog trial."""

import base64
import hmac
import logging
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .axis import MAX_BODY_BYTES, PayloadError, media_type, parse_event
from .store import Store

LOG = logging.getLogger(__name__)
CAMERAS = ("demo-entry", "demo-exit")
ID_PATTERN = r"[0-9a-f]{32}"


def camera_authorization(camera: str, token: str) -> str:
    password = hmac.digest(token.encode("ascii"), camera.encode("ascii"), "sha256").hex()
    encoded = base64.b64encode(f"{camera}:{password}".encode("ascii")).decode("ascii")
    return f"Basic {encoded}"


def authorized(header: str, scheme: str, expected: str) -> bool:
    provided_scheme, _, value = header.partition(" ")
    return provided_scheme.lower() == scheme and hmac.compare_digest(value.encode(), expected.encode())


def make_app(data_dir: Path, token: str) -> FastAPI:
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", token):
        raise ValueError("Use a demo token with 24 to 128 URL-safe characters")
    store = Store(data_dir)
    store.reconcile()
    app = FastAPI(title="PlateLog receiver demo", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        # An explicit Host check prevents a remote website from rebinding its hostname to loopback.
        host = request.headers.get("host", "")
        if not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", host, re.I):
            return Response(status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def require_reader(request: Request) -> None:
        if not authorized(request.headers.get("authorization", ""), "bearer", token):
            raise HTTPException(401, "Demo reader credential required", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/healthz")
    def health():
        return {"status": "ready", "mode": "simulation"}

    @app.post("/api/axis/{camera}/events")
    async def ingest(camera: str, request: Request):
        if camera not in CAMERAS or not authorized(
            request.headers.get("authorization", ""), "basic", camera_authorization(camera, token)[6:]
        ):
            raise HTTPException(401, "Camera credential required", headers={"WWW-Authenticate": 'Basic realm="PlateLog"'})
        content_type = request.headers.get("content-type", "")
        if media_type(content_type) not in {"application/json", "multipart/form-data"}:
            raise HTTPException(415, "Expected Axis JSON or multipart")
        content_length = request.headers.get("content-length")
        if content_length is not None and (not content_length.isascii() or not content_length.isdecimal()):
            raise HTTPException(400, "Invalid Content-Length")
        if content_length is not None and (len(content_length) > 10 or int(content_length) > MAX_BODY_BYTES):
            raise HTTPException(413, "Axis request exceeds the size limit")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                raise HTTPException(413, "Axis request exceeds the size limit")
            body.extend(chunk)
        try:
            event = await run_in_threadpool(parse_event, content_type, bytes(body))
            return await run_in_threadpool(store.save, camera, event)
        except PayloadError as exc:
            LOG.warning("Rejected invalid Axis event from %s", camera)
            raise HTTPException(400, str(exc)) from exc
        except (sqlite3.Error, OSError) as exc:
            LOG.error("Could not store Axis event from %s (%s)", camera, type(exc).__name__)
            raise HTTPException(503, "Event could not be stored") from exc

    @app.get("/api/passages")
    def list_passages(request: Request, plate: str = Query("", max_length=128),
                      camera: str = Query("", max_length=128), limit: int = Query(25, ge=1, le=100)):
        require_reader(request)
        return store.passages(plate, camera, limit)

    @app.get("/api/passages/{passage_id}")
    def passage(request: Request, passage_id: str):
        require_reader(request)
        result = store.detail(passage_id) if re.fullmatch(ID_PATTERN, passage_id) else None
        if result is None:
            raise HTTPException(404, "Passage not found")
        return result

    @app.get("/api/images/{image_id}")
    def photo(request: Request, image_id: str):
        require_reader(request)
        path = store.image_path(image_id) if re.fullmatch(ID_PATTERN, image_id) else None
        if path is None:
            raise HTTPException(404, "Image not found")
        return FileResponse(path, media_type="image/jpeg")

    return app
