"""Build a reproducible native Windows pilot ZIP from a committed Git revision."""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import re
import subprocess
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

SOURCE = Path(__file__).resolve().parents[2]
SETUP_FILES = {"Setup.ps1": "Setup.ps1", "Start-PlateLog.ps1": "Start-PlateLog.ps1",
               "Set-PlateLogCertificate.ps1": "Set-PlateLogCertificate.ps1",
               "CertificateSetup.ps1": "setup/CertificateSetup.ps1",
               "check_certificate.py": "setup/check_certificate.py", "bootstrap_admin.py": "setup/bootstrap_admin.py"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args) -> bytes:
    return subprocess.check_output(["git", "-C", str(SOURCE), *args])


def build(wheels: Path, output: Path, *, commit="HEAD", inventory: Path | None = None) -> dict:
    if output.exists() or output.with_suffix(output.suffix + ".sha256").exists():
        raise FileExistsError("Choose a new output filename; release archives are not overwritten.")
    revision = git("rev-parse", "--verify", commit + "^{commit}").decode().strip()
    tree = git("rev-parse", revision + "^{tree}").decode().strip()
    with ZipFile(io.BytesIO(git("archive", "--format=zip", revision))) as frozen:
        source = {name: frozen.read(name) for name in frozen.namelist() if not name.endswith("/")}
    assert all(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts for name in source)
    files = {}
    for name, data in source.items():
        if (name in {"LICENSE", "requirements.txt"} or name.startswith("platelog/") and name.endswith((".py", ".jpg"))
                or name.startswith("tests/test_") and name.endswith(".py") or name == "tests/certificate_setup_cases.ps1"):
            files["app/" + name] = data
        if (name in {"README.md", "IT-COMMANDS.md", "technical-spec.md", "LICENSE"}
                or name.startswith(("docs/", "examples/")) or name.startswith("deploy/") and name.endswith(".md")):
            files[name] = data
    for original, target in SETUP_FILES.items():
        files[target] = source["deploy/pilot/" + original]
    start = source["deploy/pilot/README.md"].decode("utf-8")
    start = start.replace("../../IT-COMMANDS.md", "IT-COMMANDS.md").replace("../../docs/", "docs/").replace("../../examples/", "examples/")
    files["START-HERE.md"] = (start + f"\nPackaged Git revision: `{revision}`.\n").encode()
    for name in ("SOC2-READINESS.md", "HARDENING.md"):
        files[name] = f"# PlateLog {name[:-3]}\n\nUse the current [document](docs/{name}) included in this package.\n".encode()
    inventory_bytes = inventory.read_bytes() if inventory else source["deploy/pilot/windows-x64-cp315.tsv"]
    reader = csv.DictReader(io.StringIO(inventory_bytes.decode("utf-8")), delimiter="\t")
    columns = reader.fieldnames
    rows = [row for row in reader if row["File"].startswith("wheels/")]
    versions, lock = {}, []
    for row in sorted(rows, key=lambda item: item["Name"]):
        name = Path(row["File"]).name
        if row["File"] != "wheels/" + name or not name.endswith(".whl"):
            raise ValueError("Unsafe wheel inventory path")
        data = (wheels / name).read_bytes()
        if sha(data) != row["SHA256"]:
            raise ValueError("Wheel checksum mismatch: " + name)
        with ZipFile(io.BytesIO(data)) as wheel:
            [metadata_file] = [path for path in wheel.namelist() if path.endswith(".dist-info/METADATA")]
            metadata = BytesParser(policy=policy.default).parsebytes(wheel.read(metadata_file))
        package = re.sub(r"[-_.]+", "-", metadata["Name"].lower())
        if package in versions or (package, metadata["Version"]) != (row["Name"], row["Version"]):
            raise ValueError("Wheel name/version mismatch: " + name)
        versions[package] = row["Version"]
        files[row["File"]] = data
        lock.append(f"{package}=={row['Version']} --hash=sha256:{sha(data)}\n")
    for line in source["requirements.txt"].decode().splitlines():
        name, version = line.split("==")
        if versions.get(name.lower()) != version:
            raise ValueError("Missing direct dependency: " + line)
    if len(versions) != 16:
        raise ValueError("Expected the 16 reviewed Windows CPython 3.15 wheels")
    components = io.StringIO()
    writer = csv.DictWriter(components, fieldnames=columns, delimiter="\t", lineterminator="\n")
    writer.writeheader(); writer.writerows(rows)
    files["COMPONENTS.tsv"] = components.getvalue().encode()
    files["requirements.offline.txt"] = "".join(lock).encode()
    app_paths = {name: sha(data) for name, data in files.items() if name.startswith("app/")}
    tests = sum(sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
                    for node in ast.walk(ast.parse(data)))
                for name, data in files.items() if name.startswith("app/tests/test_") and name.endswith(".py"))
    files["SOURCE-REVISION.json"] = json.dumps({"commit": revision, "tree": tree,
        "python_reference": "3.15.2", "sqlite_reference": "3.53.4", "inventory_sha256": sha(inventory_bytes),
        "tests_expected": tests, "application_files_sha256": app_paths}, sort_keys=True, indent=2).encode()
    files["SHA256SUMS.txt"] = "".join(f"{sha(data)}  {name}\n" for name, data in sorted(files.items())).encode("ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream, ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED; info.create_system = 3; info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=9)
    digest = sha(output.read_bytes())
    output.with_suffix(output.suffix + ".sha256").write_bytes(f"{digest}  {output.name}\n".encode("ascii"))
    receipt = {"source_commit": revision, "source_tree": tree, "archive": str(output.resolve()),
               "archive_sha256": digest, "archive_bytes": output.stat().st_size, "locked_wheels": len(versions),
               "manifest_entries": len(files) - 1, "tests_expected": tests, "app_paths_matched_exactly": len(app_paths),
               "setup_files_sha256": {name: sha(files[name]) for name in SETUP_FILES.values()}}
    output.parent.joinpath("Build-Receipt.json").write_bytes((json.dumps(receipt, indent=2) + "\n").encode())
    return receipt


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--wheels", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--commit", default="HEAD")
    cli.add_argument("--inventory", type=Path)
    args = cli.parse_args()
    print(json.dumps(build(args.wheels, args.output, commit=args.commit, inventory=args.inventory)))
