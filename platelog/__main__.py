"""Command line for the demo, receiver and simulator."""

import argparse
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

from .receiver import make_app
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


def run_preview(args, data_dir: Path, display_zone: ZoneInfo) -> None:
    from .preview import make_preview
    from .store import Store

    store = Store(data_dir) if args.manage_web_port else None
    port = args.port
    restarting = False
    listener = None

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

    def change_port(requested: int) -> None:
        nonlocal port, listener
        if store is None:
            raise RuntimeError("Web port control is disabled")
        reserved = bind_port(requested)
        try:
            store.set_setting("web_port", str(requested), f"Web port changed to {requested}")
        except BaseException:
            reserved.close()
            raise
        port, listener = requested, reserved

    def request_restart() -> None:
        nonlocal restarting
        restarting = True
        server.should_exit = True

    saved = store.setting("web_port") if store else None
    if saved is not None and int(saved) != port:
        try:
            listener = bind_port(int(saved))
        except OSError as exc:
            print(f"PlateLog: saved web port {saved} is unavailable ({exc}). Using port {port}.", file=sys.stderr)
            store.set_setting("web_port", str(port), f"Web port restored to {port} because port {saved} was unavailable")
        else:
            port = int(saved)
    try:
        while True:
            restarting = False
            app = make_preview(data_dir, args.allowed_host, display_zone, persistent=bool(args.data_dir),
                               allow_http_admin=args.allow_http_admin, restart=request_restart, web_port=port,
                               change_web_port=change_port if store else None)
            server = uvicorn.Server(uvicorn.Config(
                app, host=args.host, port=port, proxy_headers=False, access_log=False, timeout_graceful_shutdown=15,
                ssl_certfile=str(args.tls_cert) if args.tls_cert else None,
                ssl_keyfile=str(args.tls_key) if args.tls_key else None,
            ))
            current, listener = listener, None
            with current if current is not None else nullcontext():
                server.run(sockets=[current] if current is not None else None)
            if not restarting:
                return
    finally:
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
    preview.add_argument("--manage-web-port", action="store_true", help="Let Admin change the native web listener port")
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
            if args.manage_web_port and not args.data_dir:
                parser.error("--manage-web-port requires --data-dir.")
            scratch = nullcontext(args.data_dir) if args.data_dir else tempfile.TemporaryDirectory(prefix="platelog-preview-")
            with scratch as directory:
                data_dir = Path(directory)
                if args.data_dir:
                    from .store import Store

                    store = Store(data_dir)
                    if store.setting("preview_seeded") is None:
                        demo(data_dir)
                        store.set_setting("preview_seeded", "yes", "Demo data seeded")
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
