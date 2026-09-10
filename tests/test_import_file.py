from sync_service.import_file import HEADERS, build_rows, csv_bytes, xlsx_bytes


def test_build_rows_marks_archived_novicloud_products_off_sale():
    rows = build_rows(
        [{"id": "ms-1", "code": "A1", "name": "Item", "pathName": "Accessories",
          "archived": False, "salePrices": [{"priceType": {"name": "Cena w Polsce"}, "value": 1234}]}],
        [{"kod": "A1", "aktywny": False}],
    )
    assert rows[0][12] == "0"
    assert rows[0][8] == "12.34"


def test_export_formats_have_headers():
    rows = [[""] * len(HEADERS)]
    assert csv_bytes(rows).startswith(b"\xef\xbb\xbfBarcode")
    assert xlsx_bytes(rows).startswith(b"PK")
