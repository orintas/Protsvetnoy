import base64
import hashlib
import hmac

from sync_service.shopify_order_sync import (
    BALTIC_WAREHOUSE_CHAIN,
    COUNTRY_AGENTS,
    CURRENCY_ID,
    MAIN_STORE_ID,
    POLAND_WAREHOUSE_CHAIN,
    SHIPPING_STATE_ID,
    ULEMISTE_STORE_ID,
    WOLA_PARK_STORE_ID,
    process_new_order,
    verify_webhook_signature,
)
from sync_service.shopify_sync import ShopifySyncLog


class FakeMoySklad:
    def __init__(self, products=None, existing_order=None, stock_by_store=None):
        self.products = products or {}
        self.existing_order = existing_order
        self.stock_by_store_ = stock_by_store or {}
        self.created = None

    def customer_order_by_external_code(self, external_code):
        return self.existing_order

    def product_by_code(self, code):
        return self.products.get(code)

    def stock_by_store(self, store_id):
        return self.stock_by_store_.get(store_id, [])

    def create_customer_order(self, **kwargs):
        self.created = kwargs
        return {"id": "new-order"}


def _product(code):
    return {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/product/{code}", "type": "product"}}


def _order(order_id=1001, name="#7775", country_code="EE", line_items=None):
    return {
        "id": order_id,
        "name": name,
        "created_at": "2026-09-16T17:48:46+03:00",
        "shipping_address": {
            "country_code": country_code,
            "name": "Jane Doe",
            "address1": "Main St 1",
            "address2": "",
            "city": "Tallinn",
            "zip": "10111",
            "country": "Estonia",
            "phone": "+3725551234",
        },
        "line_items": line_items if line_items is not None else [{"sku": "ABC", "quantity": 1, "price": "14.00"}],
    }


def test_creates_order_from_main_warehouse_when_stock_available(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    moysklad = FakeMoySklad(
        products={"ABC": _product("ABC")},
        stock_by_store={MAIN_STORE_ID: [{"code": "ABC", "quantity": 5}]},
    )
    process_new_order(order=_order(), moysklad=moysklad, log=log)

    assert moysklad.created is not None
    assert moysklad.created["store_id"] == MAIN_STORE_ID
    assert moysklad.created["external_code"] == "1001"
    assert moysklad.created["agent_id"] == COUNTRY_AGENTS["EE"]
    assert "name" not in moysklad.created  # MoySklad auto-numbers it
    assert moysklad.created["description"] == "#7775\nАдрес доставки: Jane Doe, Main St 1, Tallinn, 10111, Estonia, +3725551234"
    assert moysklad.created["currency_id"] == CURRENCY_ID
    assert moysklad.created["state_id"] == SHIPPING_STATE_ID
    assert moysklad.created["positions"] == [{"quantity": 1, "price": 1400, "assortment": {"meta": _product("ABC")["meta"]}}]
    kinds = [e["kind"] for e in log.recent()]
    assert kinds == ["order_created"]


def test_falls_back_to_ulemiste_when_main_warehouse_lacks_stock(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    moysklad = FakeMoySklad(
        products={"ABC": _product("ABC")},
        stock_by_store={
            MAIN_STORE_ID: [{"code": "ABC", "quantity": 0}],
            ULEMISTE_STORE_ID: [{"code": "ABC", "quantity": 3}],
        },
    )
    process_new_order(order=_order(country_code="LV"), moysklad=moysklad, log=log)
    assert moysklad.created["store_id"] == ULEMISTE_STORE_ID
    assert moysklad.created["agent_id"] == COUNTRY_AGENTS["LV"]


def test_baltic_chain_falls_back_to_last_store_even_when_insufficient_everywhere(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    moysklad = FakeMoySklad(products={"ABC": _product("ABC")}, stock_by_store={})
    process_new_order(order=_order(country_code="FI"), moysklad=moysklad, log=log)
    assert moysklad.created["store_id"] == BALTIC_WAREHOUSE_CHAIN[-1] == ULEMISTE_STORE_ID


def test_rest_of_europe_uses_wola_park_first(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    moysklad = FakeMoySklad(
        products={"ABC": _product("ABC")},
        stock_by_store={WOLA_PARK_STORE_ID: [{"code": "ABC", "quantity": 2}]},
    )
    process_new_order(order=_order(country_code="DE"), moysklad=moysklad, log=log)
    assert moysklad.created["store_id"] == WOLA_PARK_STORE_ID
    assert moysklad.created["agent_id"] == COUNTRY_AGENTS["DE"]


def test_rest_of_europe_falls_back_to_next_poland_store(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    second_store = POLAND_WAREHOUSE_CHAIN[1]
    moysklad = FakeMoySklad(
        products={"ABC": _product("ABC")},
        stock_by_store={
            WOLA_PARK_STORE_ID: [{"code": "ABC", "quantity": 0}],
            second_store: [{"code": "ABC", "quantity": 4}],
        },
    )
    process_new_order(order=_order(country_code="FR"), moysklad=moysklad, log=log)
    assert moysklad.created["store_id"] == second_store


def test_skips_when_order_already_exists(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "already-there"})
    process_new_order(order=_order(), moysklad=moysklad, log=log)
    assert moysklad.created is None
    assert log.recent() == []


def test_unknown_shipping_country_logs_error_and_does_nothing(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    moysklad = FakeMoySklad(products={"ABC": _product("ABC")})
    process_new_order(order=_order(country_code="US"), moysklad=moysklad, log=log)
    assert moysklad.created is None
    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["kind"] == "order_error"
    assert "US" in entries[0]["message"]


def test_missing_sku_logged_but_other_positions_still_create_the_order(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    moysklad = FakeMoySklad(
        products={"ABC": _product("ABC")},
        stock_by_store={MAIN_STORE_ID: [{"code": "ABC", "quantity": 5}, {"code": "MISSING", "quantity": 5}]},
    )
    order = _order(line_items=[
        {"sku": "ABC", "quantity": 1, "price": "14.00"},
        {"sku": "MISSING", "quantity": 1, "price": "9.00"},
    ])
    process_new_order(order=order, moysklad=moysklad, log=log)
    assert moysklad.created is not None
    assert len(moysklad.created["positions"]) == 1
    error_entries = [e for e in log.recent() if e["kind"] == "order_error"]
    assert len(error_entries) == 1
    assert "MISSING" in error_entries[0]["message"]


def test_no_matching_products_at_all_skips_order_creation(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    moysklad = FakeMoySklad(products={})
    process_new_order(order=_order(), moysklad=moysklad, log=log)
    assert moysklad.created is None


def test_description_falls_back_to_order_name_when_address_is_missing(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    moysklad = FakeMoySklad(
        products={"ABC": _product("ABC")},
        stock_by_store={MAIN_STORE_ID: [{"code": "ABC", "quantity": 5}]},
    )
    order = _order()
    order["shipping_address"] = {"country_code": "EE"}  # no address fields at all
    process_new_order(order=order, moysklad=moysklad, log=log)
    assert moysklad.created["description"] == "#7775"


def test_verify_webhook_signature_accepts_correct_hmac():
    secret = "shh"
    body = b'{"id": 1}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()
    assert verify_webhook_signature(body, signature, secret) is True


def test_verify_webhook_signature_rejects_wrong_signature():
    assert verify_webhook_signature(b'{"id": 1}', "bogus==", "shh") is False


def test_verify_webhook_signature_rejects_empty_secret_or_signature():
    assert verify_webhook_signature(b"{}", "somesig", "") is False
    assert verify_webhook_signature(b"{}", "", "shh") is False
