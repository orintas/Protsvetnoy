import pytest

from sync_service.shopify_sync import ShopifyProductMap, ShopifySyncLog, sync_catalog, sync_stock


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    import sync_service.shopify_sync as mod
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)


class FakeMoySklad:
    def __init__(self, products_by_category=None, stock_by_store=None):
        self.products_by_category_ = products_by_category or {}
        self.stock_by_store_ = stock_by_store or {}

    def products_by_category(self, category):
        return self.products_by_category_.get(category, [])

    def product_image_bytes(self, product):
        return None

    def stock_by_store(self, warehouse_id):
        return self.stock_by_store_.get(warehouse_id, [])


class FakeShopify:
    def __init__(self, existing_variants=None):
        self.existing_variants = existing_variants or {}
        self.created = []
        self.updated = []
        self.inventory_sets = []

    def find_variant_by_sku(self, sku):
        return self.existing_variants.get(sku)

    def create_product(self, **kwargs):
        self.created.append(kwargs)
        return {"id": 100 + len(self.created), "variants": [{"id": 200 + len(self.created), "inventory_item_id": 300 + len(self.created)}]}

    def update_product(self, product_id, variant_id, **kwargs):
        self.updated.append((product_id, variant_id, kwargs))
        return {"id": product_id}

    def set_inventory_level(self, *, inventory_item_id, location_id, available, previous_available=None):
        self.inventory_sets.append((inventory_item_id, location_id, available))


def _product(code="ABC", name="Test Product", retail_price=1250.0, path_name="Accessories", weight=0.5, barcode="1234567890123"):
    return {
        "code": code,
        "name": name,
        "description": "desc",
        "pathName": path_name,
        "weight": weight,
        "barcodes": [{"ean13": barcode}],
        "salePrices": [
            {"value": 999.0, "priceType": {"name": "Цена оптом"}},
            {"value": retail_price, "priceType": {"name": "Цена ритэйл"}},
        ],
    }


def test_sync_catalog_creates_new_product_and_caches_mapping(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "log.sqlite3"))
    product_map = ShopifyProductMap(str(tmp_path / "map.sqlite3"))
    moysklad = FakeMoySklad(products_by_category={"Accessories": [_product()]})
    shopify = FakeShopify()

    sync_catalog(moysklad, shopify, ["Accessories"], product_map, log)

    assert len(shopify.created) == 1
    created = shopify.created[0]
    assert created["sku"] == "ABC"
    assert created["price"] == 12.5
    assert created["vendor"] == "TM Varvikas"
    assert created["product_type"] == "Accessories"

    cached = product_map.get("ABC")
    assert cached["product_id"] == 101
    assert cached["variant_id"] == 201
    assert cached["inventory_item_id"] == 301

    kinds = [e["kind"] for e in log.recent()]
    assert kinds == ["catalog_created"]


def test_sync_catalog_updates_when_shopify_already_has_the_sku(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "log.sqlite3"))
    product_map = ShopifyProductMap(str(tmp_path / "map.sqlite3"))
    moysklad = FakeMoySklad(products_by_category={"Accessories": [_product()]})
    shopify = FakeShopify(existing_variants={"ABC": {"product_id": 5, "variant_id": 6, "inventory_item_id": 7}})

    sync_catalog(moysklad, shopify, ["Accessories"], product_map, log)

    assert shopify.created == []
    assert len(shopify.updated) == 1
    assert shopify.updated[0][:2] == (5, 6)
    assert "title" not in shopify.updated[0][2]  # never touch an existing product's title
    assert product_map.get("ABC")["inventory_item_id"] == 7


def test_sync_catalog_reuses_cached_mapping_without_searching_again(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "log.sqlite3"))
    product_map = ShopifyProductMap(str(tmp_path / "map.sqlite3"))
    product_map.set("ABC", product_id=9, variant_id=10, inventory_item_id=11)
    moysklad = FakeMoySklad(products_by_category={"Accessories": [_product()]})
    shopify = FakeShopify(existing_variants={"ABC": {"product_id": 999, "variant_id": 999, "inventory_item_id": 999}})

    sync_catalog(moysklad, shopify, ["Accessories"], product_map, log)

    assert shopify.updated[0][:2] == (9, 10)


def test_sync_catalog_logs_error_when_no_retail_price(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "log.sqlite3"))
    product_map = ShopifyProductMap(str(tmp_path / "map.sqlite3"))
    product = _product()
    product["salePrices"] = [{"value": 100.0, "priceType": {"name": "Цена оптом"}}]
    moysklad = FakeMoySklad(products_by_category={"Accessories": [product]})
    shopify = FakeShopify()

    sync_catalog(moysklad, shopify, ["Accessories"], product_map, log)

    assert shopify.created == []
    error_entries = [e for e in log.recent() if e["kind"] == "catalog_error"]
    assert len(error_entries) == 1
    assert "Цена ритэйл" in error_entries[0]["message"]


def test_sync_stock_sums_across_warehouses_and_pushes_only_changed(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "log.sqlite3"))
    product_map = ShopifyProductMap(str(tmp_path / "map.sqlite3"))
    product_map.set("ABC", product_id=1, variant_id=2, inventory_item_id=3)
    product_map.set("STABLE", product_id=4, variant_id=5, inventory_item_id=6)
    product_map.set_last_available("STABLE", 10)

    moysklad = FakeMoySklad(stock_by_store={
        "wh-1": [{"code": "ABC", "quantity": 3.0}, {"code": "STABLE", "quantity": 4.0}],
        "wh-2": [{"code": "ABC", "quantity": 2.0}, {"code": "STABLE", "quantity": 6.0}],
    })
    shopify = FakeShopify()

    sync_stock(moysklad, shopify, ["wh-1", "wh-2"], location_id=999, product_map=product_map, log=log)

    assert shopify.inventory_sets == [(3, 999, 5)]
    assert product_map.get("ABC")["last_available"] == 5
    assert product_map.get("STABLE")["last_available"] == 10  # unchanged (4+6=10 already cached)


def test_sync_stock_treats_missing_sku_in_moysklad_as_zero(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "log.sqlite3"))
    product_map = ShopifyProductMap(str(tmp_path / "map.sqlite3"))
    product_map.set("SOLD_OUT", product_id=1, variant_id=2, inventory_item_id=3)
    product_map.set_last_available("SOLD_OUT", 5)
    moysklad = FakeMoySklad(stock_by_store={"wh-1": []})
    shopify = FakeShopify()

    sync_stock(moysklad, shopify, ["wh-1"], location_id=999, product_map=product_map, log=log)

    assert shopify.inventory_sets == [(3, 999, 0)]
