"""Command line for the demo, receiver and simulator."""

import argparse
import asyncio
import getpass
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn

from .receiver import DEFAULT_CAMERA_PORT, make_app, make_camera_receiver
from .simulator import call, run_scenario


def demo(data_dir: Path | None = None) -> dict:
    token = secrets.token_urlsafe(32)
    scratch = tempfile.TemporaryDirectory(prefix="platelog-demo-") if data_dir is None else nullcontext(data_dir)
    with scratch as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        config = uvicorn.Config(make_app(Path(directory), token), log_level="error", access_log=False)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                try:
                    if call(port, "/healthz")[0] == 200:
                        break
                except PermissionError:
                    raise
                except OSError:
                    pass
                if not thread.is_alive():
                    raise RuntimeError("PlateLog receiver exited before startup")
                time.sleep(0.05)
            else:
                raise RuntimeError("PlateLog receiver did not start")
            return run_scenario(port, token)
        finally:
            server.should_exit = True
            thread.join(timeout=10)


class CameraServer(uvicorn.Server):
    def capture_signals(self):
        # The staff server owns process signals for both listeners.
        return nullcontext()


def run_preview(args, data_dir: Path, display_zone: ZoneInfo) -> None:
    from .preview import make_preview
    from .store import Store

    store = Store(data_dir) if args.manage_web_port else None
    web_port = args.port
    camera_port = args.camera_port
    restarting = False
    web_socket = None
    camera_socket = None

    def bind_port(requested: int) -> socket.socket:
        sock = socket.socket()
        try:
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((args.host, requested))
        except OSError:
            sock.close()
            raise
        return sock

    def reserve_port(requested: int, setting: str, label: str) -> socket.socket:
        reserved = bind_port(requested)
        try:
            store.set_setting(setting, str(requested), f"{label} port changed to {requested}")
        except BaseException:
            reserved.close()
            raise
        return reserved

    def change_web_port(requested: int) -> None:
        nonlocal web_port, web_socket
        web_socket = reserve_port(requested, "web_port", "Web")
        web_port = requested

    def change_camera_port(requested: int) -> None:
        nonlocal camera_port, camera_socket
        camera_socket = reserve_port(requested, "camera_port", "Camera POST")
        camera_port = requested

    def request_restart() -> None:
        nonlocal restarting
        restarting = True
        server.should_exit = True

    def config(app, port: int) -> uvicorn.Config:
        return uvicorn.Config(
            app, host=args.host, port=port, proxy_headers=False, access_log=False, timeout_graceful_shutdown=15,
            ssl_certfile=str(args.tls_cert) if args.tls_cert else None,
            ssl_keyfile=str(args.tls_key) if args.tls_key else None,
        )

    async def serve_both(browser_socket: socket.socket | None, intake_socket: socket.socket | None):
        browser_sockets = [browser_socket] if browser_socket is not None else None
        if intake_socket is None:
            await server.serve(sockets=browser_sockets)
            return
        receiver = CameraServer(config(make_camera_receiver(data_dir, args.allowed_host), camera_port))
        task = asyncio.create_task(receiver.serve(sockets=[intake_socket]))
        try:
            await server.serve(sockets=browser_sockets)
        finally:
            receiver.should_exit = True
            await task

    saved_web = store.setting("web_port") if store else None
    if saved_web is not None and int(saved_web) != web_port:
        try:
            web_socket = bind_port(int(saved_web))
        except OSError as exc:
            print(f"PlateLog: saved web port {saved_web} is unavailable ({exc}). Using port {web_port}.", file=sys.stderr)
            store.set_setting("web_port", str(web_port), f"Web port restored to {web_port} because port {saved_web} was unavailable")
        else:
            web_port = int(saved_web)
    saved_camera = store.setting("camera_port") if store else None
    if saved_camera is not None:
        camera_port = int(saved_camera)
    try:
        while True:
            restarting = False
            if args.data_dir and camera_socket is None:
                if camera_port == web_port:
                    print(f"PlateLog: camera POST port {camera_port} matches the web port.", file=sys.stderr)
                else:
                    try:
                        camera_socket = bind_port(camera_port)
                    except OSError as exc:
                        print(f"PlateLog: camera POST port {camera_port} is unavailable ({exc}).", file=sys.stderr)
            app = make_preview(data_dir, args.allowed_host, display_zone, persistent=bool(args.data_dir),
                               allow_http_admin=args.allow_http_admin, restart=request_restart, web_port=web_port,
                               change_web_port=change_web_port if store else None,
                               camera_port=camera_port if args.data_dir else None, camera_online=camera_socket is not None,
                               change_camera_port=change_camera_port if store else None)
            server = uvicorn.Server(config(app, web_port))
            with (web_socket or nullcontext()) as browser_socket, (camera_socket or nullcontext()) as intake_socket:
                web_socket = camera_socket = None
                asyncio.run(serve_both(browser_socket, intake_socket))
            if not restarting:
                return
    finally:
        for listener in (web_socket, camera_socket):
            if listener is not None:
                listener.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PlateLog with synthetic Axis events")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Run the complete scenario against a temporary loopback server")
    preview = commands.add_parser("preview", help="Serve a browser preview populated by the simulator")
    preview.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "0.0.0.0"))
    preview.add_argument("--allowed-host", default="127.0.0.1", help="Accepted HTTP hostname, or '*' for any hostname")
    preview.add_argument("--timezone", default="UTC", help="IANA time zone used in the register")
    preview.add_argument("--data-dir", type=Path, help="Persistent preview data and admin settings")
    preview.add_argument("--no-demo", action="store_true", help="Start with an empty register and accept camera POSTs")
    preview.add_argument("--manage-web-port", action="store_true", help="Let Admin change native staff and camera ports")
    preview.add_argument("--camera-port", type=int, choices=range(1, 65536), default=DEFAULT_CAMERA_PORT, metavar="PORT",
                         help="TCP port for the dedicated camera receiver (default 8766)")
    preview.add_argument("--tls-cert", type=Path, help="PEM certificate for HTTPS")
    preview.add_argument("--tls-key", type=Path, help="PEM private key for HTTPS")
    admin_transport = preview.add_mutually_exclusive_group()
    admin_transport.add_argument("--allow-http-admin", action="store_true", default=True,
                                 help="Allow password-protected Admin over HTTP (default)")
    admin_transport.add_argument("--require-https-admin", action="store_false", dest="allow_http_admin",
                                 help="Require HTTPS for Admin sign-in")
    password = commands.add_parser("admin-password", help="Set or reset the administrator password in a data directory")
    password.add_argument("--data-dir", type=Path, required=True)
    serve = commands.add_parser("serve", help="Start a persistent local demo receiver")
    serve.add_argument("--data-dir", default=".platelog-data", type=Path)
    simulate = commands.add_parser("simulate", help="Send and verify synthetic events against the local receiver")
    for command in (serve, simulate, preview):
        command.add_argument("--port", default=8765, type=int, choices=range(1, 65536), metavar="PORT")
    args = parser.parse_args()

    try:
        if args.command == "demo":
            print(json.dumps(demo(), indent=2))
            return
        if args.command == "admin-password":
            from .preview import set_admin_password
            from .store import Store

            if not os.isatty(0) or not os.isatty(2):
                parser.error("Run admin-password in an interactive terminal.")
            value = getpass.getpass("New PlateLog administrator password: ")
            if value != getpass.getpass("Repeat password: "):
                parser.error("The passwords did not match.")
            set_admin_password(Store(args.data_dir), value)
            print("PlateLog administrator password saved.")
            return
        if args.command == "preview":
            try:
                display_zone = ZoneInfo(args.timezone)
            except ZoneInfoNotFoundError:
                parser.error(f"Unknown time zone: {args.timezone}")
            if bool(args.tls_cert) != bool(args.tls_key):
                parser.error("Supply both --tls-cert and --tls-key for HTTPS.")
            if (args.manage_web_port or args.no_demo) and not args.data_dir:
                parser.error("--manage-web-port and --no-demo require --data-dir.")
            scratch = nullcontext(args.data_dir) if args.data_dir else tempfile.TemporaryDirectory(prefix="platelog-preview-")
            with scratch as directory:
                data_dir = Path(directory)
                if args.data_dir:
                    from .store import Store

                    store = Store(data_dir)
                    if store.setting("preview_seeded") is None:
                        if not args.no_demo:
                            demo(data_dir)
                        store.set_setting("preview_seeded", "yes", "Register initialised" if args.no_demo else "Demo data seeded")
                else:
                    demo(data_dir)
                run_preview(args, data_dir, display_zone)
            return
        token = os.environ.get("PLATELOG_DEMO_TOKEN", "")
        if not token:
            parser.error("Set PLATELOG_DEMO_TOKEN to the same random value in both terminals")
        if args.command == "serve":
            uvicorn.run(make_app(args.data_dir, token), host="127.0.0.1", port=args.port, access_log=False)
        else:
            print(json.dumps(run_scenario(args.port, token), indent=2))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"PlateLog: {exc}\n")


if __name__ == "__main__":
    main()
