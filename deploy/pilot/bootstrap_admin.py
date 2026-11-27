"""Offline setup helper. A new Admin password arrives only through redirected stdin."""
import argparse
import json
import sqlite3
import struct
import sys
from pathlib import Path


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--check-runtime", action="store_true")
    cli.add_argument("--app-dir", type=Path)
    cli.add_argument("--data-dir", type=Path)
    args = cli.parse_args()
    if args.check_runtime:
        fixed_sqlite = sqlite3.sqlite_version_info >= (3, 51, 3) or sqlite3.sqlite_version_info[:2] == (3, 50) and sqlite3.sqlite_version_info >= (3, 50, 7) or sqlite3.sqlite_version_info[:2] == (3, 44) and sqlite3.sqlite_version_info >= (3, 44, 6)
        if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.version_info[:2] != (3, 15) or not fixed_sqlite:
            cli.exit(1, "Use approved Windows x64 Python 3.15 with a SQLite WAL-reset fix. Tested reference: Python 3.15.2 / SQLite 3.53.4.\n")
        print(json.dumps({"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version, "result": "Passed"}))
        return
    if args.app_dir is None or args.data_dir is None:
        cli.error("--app-dir and --data-dir are required")
    try:
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Invalid password input")
        password = json.loads(raw)["password"]
        if not isinstance(password, str):
            raise ValueError("Invalid password input")
        # Imports are from the manifest-checked extracted app, with -I isolation.
        sys.path.insert(0, str(args.app_dir.resolve()))
        from platelog.credentials import password_verifier
        from platelog.store import Store
        verifier = password_verifier(password)
        store = Store(args.data_dir)
        if store.setting("admin_password"):
            raise ValueError("Administrator is already configured")
        store.initialize_admin_password(verifier)
        if store.setting("admin_password") != verifier:
            raise ValueError("Administrator was configured by another process")
    except Exception as error:
        cli.exit(1, f"Admin setup failed ({type(error).__name__}). Use a new data directory and a passphrase of 14+ characters, up to 1,024 UTF-8 bytes. Existing Admin credentials are never reset by this helper.\n")
    print(json.dumps({"result": "Passed", "admin_configured": True}))


if __name__ == "__main__":
    main()
