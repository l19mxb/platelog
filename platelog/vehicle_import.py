"""Read registered plates from CSV or the first worksheet of an Excel workbook."""

import csv
import posixpath
import re
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
    for number, row in enumerate(reader, 1):
        yield number, {index: (value, True) for index, value in enumerate(row) if value.strip()}


def parse_plates(data: bytes, file_type: str) -> tuple[dict[str, str], int]:
    if file_type not in {"csv", "xlsx"}:
        raise ValueError("Choose a CSV or .xlsx file.")
    try:
        rows = excel_rows(data) if file_type == "xlsx" else csv_rows(data)
        header = next((cells for _, cells in rows if cells), {})
        columns = [column for column, (value, is_text) in header.items() if is_text and value.strip().casefold() == "plate"]
        if len(columns) != 1:
            raise ValueError("The first row needs one column named Plate. Download the CSV template for the format.")
        plates = {}
        duplicates = 0
        for number, cells in rows:
            if not cells:
                continue
            value, is_text = cells.get(columns[0], ("", True))
            plate = value.strip()
            if not is_text:
                raise ValueError(f"Row {number}: enter the plate as text. In Excel, format the Plate column as Text before entering plates.")
            key = plate_key(plate)
            if (not 1 <= len(key) <= 32 or len(plate) > 128 or
                    any(not (char.isalnum() or char in " -.") for char in plate)):
                raise ValueError(f"Row {number}: enter a plate with 1–32 letters or numbers. Spaces, dashes, and dots are allowed.")
            if key in plates:
                duplicates += 1
            else:
                plates[key] = plate
            if len(plates) + duplicates > MAX_PLATES:
                raise ValueError(f"An import can contain at most {MAX_PLATES:,} plate rows.")
        if not plates:
            raise ValueError("Add at least one plate under the Plate heading.")
    except UnicodeError as exc:
        raise ValueError("Save CSV files as UTF-8, or upload the original .xlsx workbook.") from exc
    except (BadZipFile, ET.ParseError, KeyError, csv.Error, OSError, RuntimeError, DecompressionError) as exc:
        raise ValueError("PlateLog could not read this file. Save it as CSV or a standard .xlsx workbook and try again.") from exc
    return plates, duplicates
