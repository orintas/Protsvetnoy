from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, timezone
from html import escape
from typing import Any


HEADERS = [
    "Barcode", "Name", "Type", "Tax rate", "UM", "Assortment", "Parent assortment",
    "Standard price", "Retail price", "Additional price", "Night price", "Wholesale price",
    "On sale", "CGS/CN", "Description1", "Description2", "Description3", "Description4",
    "Description5", "Symbol GTU", "Masa własna", "Ważony", "Ostatnia zmiana", "Ostatnia zmiana cen",
]
DEFAULT_CATEGORIES = (
    "Painting by numbers", "Diamond painting", "3D puzzles", "Accessories",
    "Products for Shops", "Doll making kit",
)


def _barcode(product: dict[str, Any]) -> str:
    barcodes = product.get("barcodes") or []
    if isinstance(barcodes, list) and barcodes and isinstance(barcodes[0], dict):
        return str(barcodes[0].get("ean13") or barcodes[0].get("gtin") or "")
    return str(product.get("code") or "")


def _price(product: dict[str, Any]) -> float:
    prices = product.get("salePrices") or []
    if isinstance(prices, list):
        for item in prices:
            if isinstance(item, dict) and str(item.get("priceType", {}).get("name", "")).lower() == "cena w polsce":
                return round(float(item.get("value", 0)) / 100, 2)
    return 0.0


def build_rows(moysklad: list[dict[str, Any]], novicloud: list[dict[str, Any]], categories: tuple[str, ...] = DEFAULT_CATEGORIES) -> list[list[str]]:
    active_by_code = {str(item.get("kod", "")): item for item in novicloud}
    rows: list[list[str]] = []
    for product in moysklad:
        category = str(product.get("pathName") or "")
        if category not in categories:
            continue
        code = str(product.get("code") or "")
        novicloud_item = active_by_code.get(code)
        if novicloud_item is None:
            continue
        archived = bool(product.get("archived")) or novicloud_item.get("aktywny") is False
        price = _price(product)
        barcode = _barcode(product)
        name = f"{code} - {product.get('name', '')}".strip(" -")
        changed = str(product.get("updated", ""))
        rows.append([
            barcode, name, "0", "23.00", "szt", category, "",
            "0", str(price), str(price), str(price), str(price), "0" if archived else "1",
            code, "", "", str(product.get("id", "")), "", "", "", "", "", changed, "",
        ])
    return sorted(rows, key=lambda row: row[1].lower())


def csv_bytes(rows: list[list[str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def xlsx_bytes(rows: list[list[str]]) -> bytes:
    values = [HEADERS, *rows]
    cells = []
    for r, row in enumerate(values, 1):
        cells.append(f'<row r="{r}">' + "".join(
            f'<c r="{chr(65 + c)}{r}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
            for c, value in enumerate(row)
        ) + "</row>")
    sheet = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(cells) + "</sheetData></worksheet>"
    content_types = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    workbook = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Novicloud import" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    workbook_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return result.getvalue()
