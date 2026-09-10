from sync_service.import_file import (
    HEADERS,
    build_rows,
    compare_catalogs,
    csv_bytes,
    rows_for_codes,
    xlsx_bytes,
)


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


def test_compare_catalogs_reports_missing_and_price_changes():
    moysklad = [
        {"id": "1", "code": "NEW", "name": "New", "pathName": "Accessories", "salePrices": []},
        {
            "id": "2",
            "code": "PRICE",
            "name": "Price",
            "pathName": "Accessories",
            "salePrices": [{"priceType": {"name": "Cena w Polsce"}, "value": 1000}],
        },
    ]
    novicloud = [{"kod": "PRICE", "cena_det": 5, "aktywny": True}]
    result = compare_catalogs(moysklad, novicloud)
    assert {row["status"] for row in result} == {"missing", "price"}
    assert rows_for_codes(moysklad, novicloud, {"NEW"})[0][1].startswith("NEW -")


def test_build_rows_reads_russian_polish_price_type():
    rows = build_rows(
        [{
            "id": "ms-2",
            "code": "A2",
            "name": "Item",
            "pathName": "Accessories",
            "salePrices": [{"priceType": {"name": "Цена в Польше"}, "value": "999,00"}],
        }],
        [{"kod": "A2", "aktywny": True}],
    )
    assert rows[0][8] == "9.99"
