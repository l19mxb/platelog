"""Build the Windows x64 / Python 3.13 PlateLog offline handoff ZIP."""

import argparse
import hashlib
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


SOURCE = Path(__file__).resolve().parents[2]
APP_FILES = (
    "README.md", "LICENSE", "technical-spec.md", "requirements.txt", "deploy/windows/README.md",
    "docs/images/register.png", "docs/images/register-search.png", "docs/images/demo-l19mxb.jpg",
    "docs/images/admin-csv-folder.png",
    "examples/Send-AxisEvent.ps1",
    "deploy/windows/PlateLog.xml", "platelog/__init__.py", "platelog/__main__.py",
    "platelog/alarm_admin.py", "platelog/alarms.py", "platelog/axis.py", "platelog/credentials.py",
    "platelog/daily_csv.py", "platelog/preview.py", "platelog/receiver.py",
    "platelog/register_stats.py", "platelog/registered_refresh.py", "platelog/simulator.py", "platelog/store.py",
    "platelog/vehicle_import.py",
    "platelog/fixtures/entry.jpg", "platelog/fixtures/exit.jpg",
    "tests/test_alarms.py", "tests/test_receiver.py", "tests/test_registered_vehicles.py",
)
VENDOR_FILES = (
    ("python-3.13.17-amd64.exe", "Python", "3.13.17",
     "https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe",
     "5da012101ba2e17113a9a1cd901e9ce1441086f2b7ecb5de06ec94163899a190"),
    ("WinSW.NET4.exe", "WinSW", "2.12.0",
     "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET4.exe",
     "923111c7142b3dc783a3c722b19b8a21bcb78222d7a136ac33f0ca8a29f4cb66"),
    ("WinSW-LICENSE.txt", "WinSW license", "2.12.0",
     "https://raw.githubusercontent.com/winsw/winsw/v2.12.0/LICENSE.txt",
     "1cdf703c10a70e5973bf3acf2a5eeabe7746237155b92db2034aeae26fdf7802"),
)


def read_wheel(path: Path) -> tuple[str, str]:
    platform = path.stem.rsplit("-", 1)[-1]
    if platform not in {"any", "win_amd64"}:
        raise ValueError(f"Unexpected wheel platform: {path.name}")
    with ZipFile(path) as wheel:
        [metadata_path] = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        metadata = BytesParser(policy=policy.default).parsebytes(wheel.read(metadata_path))
    name = re.sub(r"[-_.]+", "-", metadata["Name"].lower())
    version = metadata["Version"]
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not re.fullmatch(r"[0-9][0-9A-Za-z.+!-]*", version):
        raise ValueError(f"Unexpected wheel metadata: {path.name}")
    return name, version


def build(wheels: Path, vendor: Path, output: Path) -> None:
    files = {f"app/{name}": (SOURCE / name).read_bytes() for name in APP_FILES}
    files["START-HERE.md"] = files["app/deploy/windows/README.md"]
    components = ["Name\tVersion\tFile\tSource\tSHA256"]
    for filename, name, version, url, expected in VENDOR_FILES:
        path = f"vendor/{filename}"
        files[path] = (vendor / filename).read_bytes()
        observed = hashlib.sha256(files[path]).hexdigest()
        if observed != expected:
            raise ValueError(f"Vendor checksum mismatch: {filename}")
        components.append(f"{name}\t{version}\t{path}\t{url}\t{observed}")

    lock = {}
    versions = {}
    for wheel in sorted(wheels.glob("*.whl")):
        name, version = read_wheel(wheel)
        if name in lock:
            raise ValueError(f"Multiple wheels for {name}")
        path = f"wheels/{wheel.name}"
        files[path] = wheel.read_bytes()
        digest = hashlib.sha256(files[path]).hexdigest()
        versions[name] = version
        lock[name] = f"{name}=={version} --hash=sha256:{digest}"
        components.append(f"{name}\t{version}\t{path}\thttps://pypi.org/project/{name}/{version}/\t{digest}")

    for line in (SOURCE / "requirements.txt").read_text().splitlines():
        if line.strip():
            name, version = line.split("==")
            if versions.get(name.lower()) != version:
                raise ValueError(f"Missing required wheel: {line}")
    files["requirements.offline.txt"] = ("\n".join(lock[name] for name in sorted(lock)) + "\n").encode()
    files["COMPONENTS.tsv"] = ("\n".join(components) + "\n").encode()
    files["SHA256SUMS.txt"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items())
    ).encode()
    # Setup is covered by the ZIP hash so IT can sign it after extraction.
    files["Setup.ps1"] = (SOURCE / "deploy/windows/Setup.ps1").read_bytes()

    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as bundle:
        for name, data in sorted(files.items()):
            bundle.writestr(name, data)
    with output.open("rb") as archive:
        digest = hashlib.file_digest(archive, "sha256").hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--vendor", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.wheels, args.vendor, args.output)


if __name__ == "__main__":
    main()
