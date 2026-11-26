"""Read registered plates from CSV or the first worksheet of an Excel workbook."""

import csv
import posixpath
import re
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile
from zlib import error as DecompressionError

from .axis import plate_key

IMPORT_BYTES = 4 * 1024 * 1024
MAX_PLATES = 25_000
SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
OFFICE_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PACKAGE_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


@dataclass
class PlateImport:
    plates: dict[str, str] = field(default_factory=dict)
    duplicates: int = 0
    source_format: str = "plates"
    rows: int = 0
    excluded_rows: int = 0
    skipped_entries: int = 0
    spaces_removed: int = 0
    shared_plates: int = 0
    warnings: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    source_rows: dict[int, str] = field(default_factory=dict)

    def report(self) -> dict:
        return {"format": self.source_format, "rows": self.rows, "duplicates": self.duplicates,
                "excluded_rows": self.excluded_rows, "skipped_entries": self.skipped_entries,
                "spaces_removed": self.spaces_removed, "shared_plates": self.shared_plates,
                "skipped_rows": len(self.source_rows),
                "warnings": self.warnings.copy()}

    def skip(self, number: int, entry_number: int, entry: str, vehicle: str, reason: str) -> None:
        self.skipped_entries += 1
        self.source_rows[number] = vehicle
        self.skipped.append({"row": number, "entry_number": entry_number, "entry": entry, "reason": reason})
        if len(self.warnings) < 20:
            self.warnings.append(f"Row {number}: {reason}")

    def add(self, key: str, plate: str) -> None:
        if key in self.plates:
            self.duplicates += 1
        else:
            self.plates[key] = plate


class ImportRejected(ValueError):
    """A rejected import can still supply skipped entries for administrator review."""

    def __init__(self, message: str, imported: PlateImport | None):
        super().__init__(message)
        self.imported = imported


def xml_part(book: ZipFile, path: str, limit: int) -> ET.Element:
    info = book.getinfo(path)
    if info.compress_type not in {ZIP_DEFLATED, ZIP_STORED}:
        raise ValueError("The workbook uses an unsupported compression method. Save it as .xlsx or CSV.")
    if info.file_size > limit:
        raise ValueError("An Excel worksheet or workbook part is too large. Try saving the list as CSV.")
    with book.open(info) as part:
        data = part.read(limit + 1)
    if len(data) > limit:
        raise ValueError("An Excel worksheet or workbook part is too large. Try saving the list as CSV.")
    xml = data.decode("utf-8-sig")
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise ValueError("The workbook contains an unsupported XML declaration.")
    return ET.fromstring(xml)


def excel_rows(data: bytes):
    with ZipFile(BytesIO(data)) as book:
        workbook = xml_part(book, "xl/workbook.xml", 256 * 1024)
        first_sheet = workbook.find(f"{SHEET_NS}sheets/{SHEET_NS}sheet")
        links = xml_part(book, "xl/_rels/workbook.xml.rels", 256 * 1024)
        paths = {}
        for link in links.findall(f"{PACKAGE_NS}Relationship"):
            if link.get("TargetMode", "").lower() != "external":
                target = link.attrib["Target"]
                path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else f"xl/{target}")
                paths[link.attrib["Id"]] = (link.attrib["Type"], path)
        if first_sheet is None:
            raise ValueError("The workbook has no worksheet.")
        kind, path = paths[first_sheet.attrib[f"{OFFICE_NS}id"]]
        if kind != f"{OFFICE_NS[1:-1]}/worksheet" or not path.startswith("xl/"):
            raise ValueError("The first tab in the workbook must be a worksheet.")
        sheet = xml_part(book, path, 12 * 1024 * 1024)
        shared = next((path for kind, path in paths.values() if kind == f"{OFFICE_NS[1:-1]}/sharedStrings"), None)
        strings = []
        if shared:
            root = xml_part(book, shared, 12 * 1024 * 1024)
            strings = ["".join(node.text or "" for node in item.iter(f"{SHEET_NS}t"))
                       for item in root.findall(f"{SHEET_NS}si")]
        for number, row in enumerate(sheet.findall(f"{SHEET_NS}sheetData/{SHEET_NS}row"), 1):
            cells = {}
            for cell in row.findall(f"{SHEET_NS}c"):
                reference = re.fullmatch(r"([A-Z]{1,3})[1-9][0-9]*", cell.attrib["r"])
                if not reference:
                    raise ValueError("The workbook contains an invalid cell reference.")
                value = cell.findtext(f"{SHEET_NS}v", "")
                kind = cell.get("t")
                formula = cell.find(f"{SHEET_NS}f") is not None
                if kind == "s":
                    index = int(value)
                    if not 0 <= index < len(strings):
                        raise ValueError("The workbook contains an invalid shared text reference.")
                    value = strings[index]
                elif kind == "inlineStr":
                    value = "".join(node.text or "" for node in cell.iter(f"{SHEET_NS}t"))
                is_text = not formula and kind in {"s", "str", "inlineStr"}
                if value.strip() or formula:
                    cells[reference[1]] = (value, is_text)
            yield int(row.get("r", number)), cells


def csv_rows(data: bytes):
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    reader = csv.reader(StringIO(data.decode(encoding), newline=""), strict=True)
    width = None
    for number, row in enumerate(reader, 1):
        if any(value.strip() for value in row):
            if width is None:
                width = len(row)
            elif len(row) > width:
                raise ValueError(f"Row {number}: there are more CSV columns than headings. Quote fields containing commas.")
        yield number, {index: (value, True) for index, value in enumerate(row) if value.strip()}


def cardholder_plates(rows, vehicle_column, authorised_column, result: PlateImport) -> PlateImport:
    unauthorised = set()
    entries = 0
    for number, cells in rows:
        if not cells:
            continue
        result.rows += 1
        if result.rows > MAX_PLATES:
            raise ValueError(f"An import can contain at most {MAX_PLATES:,} cardholder rows.")
        status, is_text = cells.get(authorised_column, ("", True))
        if not is_text or status.strip().casefold() not in {"yes", "no"}:
            raise ValueError(f"Row {number}: Cardholder Authorised must contain Yes or No.")
        authorised = status.strip().casefold() == "yes"
        if not authorised:
            result.excluded_rows += 1
        value, is_text = cells.get(vehicle_column, ("", True))
        if not is_text:
            if authorised:
                result.skip(number, 1, value, value, "Vehicle registrations must be text to preserve leading zeros.")
                raise ValueError(f"Row {number}: enter Vehicle registrations as text to keep leading zeros.")
            continue
        for entry_number, entry in enumerate(value.split(","), 1):
            entries += 1
            if entries > MAX_PLATES:
                raise ValueError(f"An import can contain at most {MAX_PLATES:,} registration entries.")
            trimmed = entry.strip()
            compact = "".join(trimmed.split())
            plate = compact.upper()
            valid = compact.isascii() and plate.isalnum() and 1 <= len(plate) <= 32
            if not authorised:
                if valid:
                    unauthorised.add(plate_key(plate))
                continue
            if not valid:
                reason = ("Empty registration." if not plate else
                          "Registration exceeds 32 characters." if len(plate) > 32 else
                          "Contains characters other than letters and numbers. Use commas between registrations.")
                result.skip(number, entry_number, entry, value, reason)
                continue
            if any(char.isspace() for char in trimmed):
                result.spaces_removed += 1
            result.add(plate_key(plate), plate)
    result.shared_plates = len(result.plates.keys() & unauthorised)
    if not result.plates:
        raise ValueError("No authorised registrations were found. Check Vehicle and Cardholder Authorised.")
    return result


def parse_plate_import(data: bytes, file_type: str) -> PlateImport:
    if file_type not in {"csv", "xlsx"}:
        raise ValueError("Choose a CSV or .xlsx file.")
    if len(data) > IMPORT_BYTES:
        raise ValueError("Plate lists can be up to 4 MiB. Save large workbooks as CSV.")
    result = None
    try:
        rows = excel_rows(data) if file_type == "xlsx" else csv_rows(data)
        header = next((cells for _, cells in rows if cells), {})
        def columns_named(name):
            return [column for column, (value, is_text) in header.items()
                    if is_text and value.strip().casefold() == name]

        columns = columns_named("plate")
        vehicle = columns_named("vehicle")
        authorised = columns_named("cardholder authorised")
        if authorised or (vehicle and not columns):
            if columns or len(vehicle) != 1 or len(authorised) != 1:
                raise ValueError("Use one Vehicle column and one Cardholder Authorised column, or a separate Plate list.")
            result = PlateImport(source_format="cardholders")
            return cardholder_plates(rows, vehicle[0], authorised[0], result)
        if len(columns) != 1:
            raise ValueError("The first row needs a Plate column, or Vehicle and Cardholder Authorised columns.")
        result = PlateImport()
        for number, cells in rows:
            if not cells:
                continue
            result.rows += 1
            value, is_text = cells.get(columns[0], ("", True))
            plate = value.strip()
            if not is_text:
                result.skip(number, 1, value, value, "Plate must be text to preserve leading zeros.")
                raise ValueError(f"Row {number}: enter the plate as text. In Excel, format the Plate column as Text before entering plates.")
            key = plate_key(plate)
            if (not 1 <= len(key) <= 32 or len(plate) > 128 or
                    any(not (char.isalnum() or char in " -.") for char in plate)):
                result.skip(number, 1, value, value, "Use 1–32 letters or numbers. Spaces, dashes, and dots are allowed in a Plate list.")
                raise ValueError(f"Row {number}: enter a plate with 1–32 letters or numbers. Spaces, dashes, and dots are allowed.")
            result.add(key, plate)
            if result.rows > MAX_PLATES:
                raise ValueError(f"An import can contain at most {MAX_PLATES:,} plate rows.")
        if not result.plates:
            raise ValueError("Add at least one plate under the Plate heading.")
    except UnicodeError as exc:
        raise ImportRejected("Save CSV files as UTF-8, or upload the original .xlsx workbook.", result) from exc
    except (BadZipFile, ET.ParseError, KeyError, csv.Error, OSError, RuntimeError, DecompressionError) as exc:
        raise ImportRejected("PlateLog could not read this file. Save it as CSV or a standard .xlsx workbook and try again.", result) from exc
    except ValueError as exc:
        raise ImportRejected(str(exc), result) from exc
    return result


def parse_plates(data: bytes, file_type: str) -> tuple[dict[str, str], int]:
    result = parse_plate_import(data, file_type)
    return result.plates, result.duplicates
