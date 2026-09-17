from sync_service.ozon_stock_sync import OZON_WAREHOUSES, AssortmentCache, run_once, sync_warehouse_stock
from sync_service.yandex_market_sync import YandexMarketSyncLog


class FakeSettings:
    moysklad_base_url = "https://api.moysklad.ru/api/remap/1.2"
    moysklad_token = "test-token"
    ozon_client_id = "test-client-id"
    ozon_api_key = "test-api-key"


class FakeMoySklad:
    def __init__(self, rows_by_store=None, fail_stores=None):
        self.rows_by_store = rows_by_store or {}
        self.fail_stores = fail_stores or set()
        self.closed = False

    def stock_by_store(self, store_id):
        if store_id in self.fail_stores:
            raise RuntimeError("boom moysklad")
        return self.rows_by_store.get(store_id, [])

    def close(self):
        self.closed = True


class FakeOzon:
    def __init__(self, offer_ids=None, fail_warehouses=None):
        self.offer_ids_ = offer_ids or []
        self.fail_warehouses = fail_warehouses or set()
        self.calls = []
        self.warehouse_ids_used = []
        self.product_offer_ids_calls = 0
        self.closed = False

    def product_offer_ids(self):
        self.product_offer_ids_calls += 1
        return self.offer_ids_

    def update_stocks(self, items, *, warehouse_id):
        if warehouse_id in self.fail_warehouses:
            raise RuntimeError("boom ozon")
        self.calls.append((warehouse_id, items))
        self.warehouse_ids_used.append(warehouse_id)

    def close(self):
        self.closed = True


def _patched(monkeypatch, moysklad, ozon):
    import sync_service.ozon_stock_sync as mod
    monkeypatch.setattr(mod, "MoySkladClient", lambda **kwargs: moysklad)
    monkeypatch.setattr(mod, "OzonClient", lambda **kwargs: ozon)


def test_sync_warehouse_stock_refreshes_stale_cache_and_pushes_zero_for_missing_offers(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    moysklad = FakeMoySklad(rows_by_store={"store-1": [{"code": "RB016", "quantity": 3.0}]})
    ozon = FakeOzon(offer_ids=["RB016", "SOLD_OUT_SKU"])

    count, changes = sync_warehouse_stock(moysklad, ozon, cache, warehouse_id=111, store_id="store-1")

    assert count == 2
    assert changes is None  # first sync: no prior state to diff against
    assert ozon.product_offer_ids_calls == 1
    assert ozon.calls == [(111, [{"sku": "RB016", "count": 3}, {"sku": "SOLD_OUT_SKU", "count": 0}])]
    assert ozon.warehouse_ids_used == [111]


def test_known_warehouses_map_to_the_same_stores_yandex_market_uses(tmp_path):
    from sync_service.yandex_market_order_sync import CAMPAIGN_STORES
    mall_store_ids = set(OZON_WAREHOUSES.values()) & set(CAMPAIGN_STORES.values())
    assert len(mall_store_ids) == 4  # the 4 shared malls; the 5th OZON warehouse (main) has no Yandex counterpart


def test_sync_warehouse_stock_reports_changes_against_previous_run(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    ozon = FakeOzon(offer_ids=["RB016", "STABLE"])

    moysklad_first = FakeMoySklad(rows_by_store={"store-1": [{"code": "RB016", "quantity": 3.0}, {"code": "STABLE", "quantity": 7.0}]})
    sync_warehouse_stock(moysklad_first, ozon, cache, warehouse_id=111, store_id="store-1")

    moysklad_second = FakeMoySklad(rows_by_store={"store-1": [{"code": "RB016", "quantity": 1.0}, {"code": "STABLE", "quantity": 7.0}]})
    count, changes = sync_warehouse_stock(moysklad_second, ozon, cache, warehouse_id=111, store_id="store-1")

    assert count == 2
    assert changes == [{"sku": "RB016", "before": 3, "after": 1}]
    assert ozon.calls[-1] == (111, [{"sku": "RB016", "count": 1}])


def test_sync_warehouse_stock_sends_nothing_when_nothing_changed(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    ozon = FakeOzon(offer_ids=["RB016"])
    moysklad = FakeMoySklad(rows_by_store={"store-1": [{"code": "RB016", "quantity": 3.0}]})

    sync_warehouse_stock(moysklad, ozon, cache, warehouse_id=111, store_id="store-1")
    count, changes = sync_warehouse_stock(moysklad, ozon, cache, warehouse_id=111, store_id="store-1")

    assert count == 1
    assert changes == []
    assert len(ozon.calls) == 1  # second sync had nothing to push


def test_sync_warehouse_stock_clamps_negative_quantity_to_zero(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    moysklad = FakeMoySklad(rows_by_store={"store-1": [{"code": "RB016", "quantity": -2.0}]})
    ozon = FakeOzon(offer_ids=["RB016"])
    sync_warehouse_stock(moysklad, ozon, cache, warehouse_id=111, store_id="store-1")
    assert ozon.calls == [(111, [{"sku": "RB016", "count": 0}])]


def test_run_once_syncs_every_known_warehouse(tmp_path, monkeypatch):
    log = YandexMarketSyncLog(str(tmp_path / "ozon.sqlite3"))
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    rows_by_store = {store_id: [{"code": "X", "quantity": 1.0}] for store_id in OZON_WAREHOUSES.values()}
    moysklad = FakeMoySklad(rows_by_store=rows_by_store)
    ozon = FakeOzon(offer_ids=["X"])
    _patched(monkeypatch, moysklad, ozon)

    run_once(FakeSettings(), log, cache)

    synced_warehouses = {call[0] for call in ozon.calls}
    assert synced_warehouses == set(OZON_WAREHOUSES)
    entries = log.recent()
    assert len(entries) == len(OZON_WAREHOUSES)
    assert all(e["kind"] == "ozon_stock_sync" and e["status"] == "success" for e in entries)
    assert moysklad.closed and ozon.closed


def test_run_once_logs_error_for_one_warehouse_but_continues_others(tmp_path, monkeypatch):
    log = YandexMarketSyncLog(str(tmp_path / "ozon.sqlite3"))
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    failing_warehouse = next(iter(OZON_WAREHOUSES))
    failing_store = OZON_WAREHOUSES[failing_warehouse]
    rows_by_store = {store_id: [{"code": "X", "quantity": 1.0}] for store_id in OZON_WAREHOUSES.values()}
    moysklad = FakeMoySklad(rows_by_store=rows_by_store, fail_stores={failing_store})
    ozon = FakeOzon(offer_ids=["X"])
    _patched(monkeypatch, moysklad, ozon)

    run_once(FakeSettings(), log, cache)

    synced_warehouses = {call[0] for call in ozon.calls}
    assert synced_warehouses == set(OZON_WAREHOUSES) - {failing_warehouse}
    entries = log.recent()
    error_entries = [e for e in entries if e["status"] == "error"]
    assert len(error_entries) == 1
