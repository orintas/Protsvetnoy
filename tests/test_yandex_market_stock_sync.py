import sync_service.change_log as change_log_module
from sync_service.yandex_market_order_sync import CAMPAIGN_STORES, CAMPAIGN_WAREHOUSES
from sync_service.yandex_market_stock_sync import AssortmentCache, run_once, sync_campaign_stock
from sync_service.yandex_market_sync import YandexMarketSyncLog


class FakeSettings:
    moysklad_base_url = "https://api.moysklad.ru/api/remap/1.2"
    moysklad_token = "test-token"
    yandex_market_base_url = "https://api.partner.market.yandex.ru"
    yandex_market_api_key = "test-key"
    yandex_market_business_id = "939642"


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


class FakeYandex:
    def __init__(self, offers_by_campaign=None, fail_campaigns=None):
        self.offers_by_campaign = offers_by_campaign or {}
        self.fail_campaigns = fail_campaigns or set()
        self.calls = []
        self.warehouse_ids_used = []
        self.campaign_offers_calls = []
        self.closed = False

    def campaign_offers(self, campaign_id):
        self.campaign_offers_calls.append(campaign_id)
        return self.offers_by_campaign.get(campaign_id, [])

    def update_stocks(self, items, *, campaign_id, warehouse_id):
        if campaign_id in self.fail_campaigns:
            raise RuntimeError("boom yandex")
        self.calls.append((campaign_id, items))
        self.warehouse_ids_used.append(warehouse_id)

    def close(self):
        self.closed = True


def _patched(monkeypatch, moysklad, yandex):
    import sync_service.yandex_market_stock_sync as mod
    monkeypatch.setattr(mod, "MoySkladClient", lambda **kwargs: moysklad)
    monkeypatch.setattr(mod, "YandexMarketClient", lambda **kwargs: yandex)


def test_sync_campaign_stock_refreshes_stale_cache_and_pushes_zero_for_missing_offers(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    moysklad = FakeMoySklad(rows_by_store={"store-1": [{"code": "RGL02", "quantity": 3.0}]})
    yandex = FakeYandex(offers_by_campaign={"149179260": ["RGL02", "SOLD_OUT_SKU"]})

    count, changes = sync_campaign_stock(moysklad, yandex, cache, campaign_id="149179260", store_id="store-1")

    assert count == 2
    assert changes is None  # first sync for this campaign: no prior state to diff against
    assert yandex.campaign_offers_calls == ["149179260"]
    assert yandex.calls == [("149179260", [{"sku": "RGL02", "count": 3}, {"sku": "SOLD_OUT_SKU", "count": 0}])]
    assert yandex.warehouse_ids_used == [CAMPAIGN_WAREHOUSES["149179260"]]


def test_known_campaigns_have_a_warehouse_mapping():
    assert set(CAMPAIGN_WAREHOUSES) == set(CAMPAIGN_STORES)


def test_sync_campaign_stock_reports_changes_against_previous_run(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    yandex = FakeYandex(offers_by_campaign={"149179260": ["RGL02", "STABLE"]})

    moysklad_first = FakeMoySklad(rows_by_store={"store-1": [{"code": "RGL02", "quantity": 3.0}, {"code": "STABLE", "quantity": 7.0}]})
    sync_campaign_stock(moysklad_first, yandex, cache, campaign_id="149179260", store_id="store-1")

    moysklad_second = FakeMoySklad(rows_by_store={"store-1": [{"code": "RGL02", "quantity": 1.0}, {"code": "STABLE", "quantity": 7.0}]})
    count, changes = sync_campaign_stock(moysklad_second, yandex, cache, campaign_id="149179260", store_id="store-1")

    assert count == 2
    assert changes == [{"sku": "RGL02", "before": 3, "after": 1}]
    # only the changed sku was actually sent to Yandex, not the unchanged STABLE
    assert yandex.calls[-1] == ("149179260", [{"sku": "RGL02", "count": 1}])

    # first sync logged 2 "create" rows (baseline for RGL02 and STABLE); this second
    # sync's actual change is the most recent row
    update_entries = [e for e in change_log_module._instance.recent() if e["action"] == "update"]
    assert len(update_entries) == 1
    assert update_entries[0]["entity_id"] == "149179260:RGL02"
    assert update_entries[0]["before"] == "3"
    assert update_entries[0]["after"] == "1"


def test_sync_campaign_stock_sends_nothing_when_nothing_changed(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    yandex = FakeYandex(offers_by_campaign={"149179260": ["RGL02"]})
    moysklad = FakeMoySklad(rows_by_store={"store-1": [{"code": "RGL02", "quantity": 3.0}]})

    sync_campaign_stock(moysklad, yandex, cache, campaign_id="149179260", store_id="store-1")
    count, changes = sync_campaign_stock(moysklad, yandex, cache, campaign_id="149179260", store_id="store-1")

    assert count == 1
    assert changes == []
    # nothing to push means update_stocks isn't even called a second time
    assert len(yandex.calls) == 1


def test_sync_campaign_stock_reports_zero_as_before_for_a_newly_added_offer(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))

    yandex = FakeYandex(offers_by_campaign={"149179260": ["RGL02"]})
    moysklad_first = FakeMoySklad(rows_by_store={"store-1": [{"code": "RGL02", "quantity": 3.0}]})
    sync_campaign_stock(moysklad_first, yandex, cache, campaign_id="149179260", store_id="store-1")

    # simulate the daily assortment refresh picking up a newly listed offer
    cache.replace("149179260", ["RGL02", "NEW_SKU"])
    moysklad_second = FakeMoySklad(rows_by_store={"store-1": [{"code": "RGL02", "quantity": 3.0}, {"code": "NEW_SKU", "quantity": 5.0}]})
    count, changes = sync_campaign_stock(moysklad_second, yandex, cache, campaign_id="149179260", store_id="store-1")

    assert count == 2
    assert changes == [{"sku": "NEW_SKU", "before": None, "after": 5}]


def test_sync_campaign_stock_reuses_fresh_cache_without_refetching_assortment(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    cache.replace("149179260", ["RGL02"])
    moysklad = FakeMoySklad(rows_by_store={"store-1": [{"code": "RGL02", "quantity": 5.0}]})
    yandex = FakeYandex(offers_by_campaign={"149179260": ["RGL02", "SHOULD_NOT_APPEAR"]})

    sync_campaign_stock(moysklad, yandex, cache, campaign_id="149179260", store_id="store-1")

    assert yandex.campaign_offers_calls == []
    assert yandex.calls == [("149179260", [{"sku": "RGL02", "count": 5}])]


def test_sync_campaign_stock_clamps_negative_quantity_to_zero(tmp_path):
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    moysklad = FakeMoySklad(rows_by_store={"store-1": [{"code": "RGL02", "quantity": -2.0}]})
    yandex = FakeYandex(offers_by_campaign={"149179260": ["RGL02"]})
    sync_campaign_stock(moysklad, yandex, cache, campaign_id="149179260", store_id="store-1")
    assert yandex.calls == [("149179260", [{"sku": "RGL02", "count": 0}])]


def test_run_once_syncs_every_known_campaign(tmp_path, monkeypatch):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    rows_by_store = {store_id: [{"code": "X", "quantity": 1.0}] for store_id in CAMPAIGN_STORES.values()}
    moysklad = FakeMoySklad(rows_by_store=rows_by_store)
    yandex = FakeYandex(offers_by_campaign={c: ["X"] for c in CAMPAIGN_STORES})
    _patched(monkeypatch, moysklad, yandex)

    run_once(FakeSettings(), log, cache)

    synced_campaigns = {call[0] for call in yandex.calls}
    assert synced_campaigns == set(CAMPAIGN_STORES)
    entries = log.recent()
    assert len(entries) == len(CAMPAIGN_STORES)
    assert all(e["kind"] == "stock_sync" and e["status"] == "success" for e in entries)
    assert moysklad.closed and yandex.closed


def test_run_once_logs_error_for_one_campaign_but_continues_others(tmp_path, monkeypatch):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    failing_store = CAMPAIGN_STORES["149179260"]
    rows_by_store = {store_id: [{"code": "X", "quantity": 1.0}] for store_id in CAMPAIGN_STORES.values()}
    moysklad = FakeMoySklad(rows_by_store=rows_by_store, fail_stores={failing_store})
    yandex = FakeYandex(offers_by_campaign={c: ["X"] for c in CAMPAIGN_STORES})
    _patched(monkeypatch, moysklad, yandex)

    run_once(FakeSettings(), log, cache)

    synced_campaigns = {call[0] for call in yandex.calls}
    assert synced_campaigns == set(CAMPAIGN_STORES) - {"149179260"}
    entries = log.recent()
    error_entries = [e for e in entries if e["status"] == "error"]
    assert len(error_entries) == 1
    assert "ТЦ Ривьера" in error_entries[0]["message"]


def test_run_once_message_lists_changed_skus_before_and_after(tmp_path, monkeypatch):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    store_id = CAMPAIGN_STORES["149179260"]
    yandex = FakeYandex(offers_by_campaign={"149179260": ["RGL02"]})
    moysklad_first = FakeMoySklad(rows_by_store={store_id: [{"code": "RGL02", "quantity": 3.0}]})
    _patched(monkeypatch, moysklad_first, yandex)
    run_once(FakeSettings(), log, cache)

    moysklad_second = FakeMoySklad(rows_by_store={store_id: [{"code": "RGL02", "quantity": 0.0}]})
    _patched(monkeypatch, moysklad_second, yandex)
    run_once(FakeSettings(), log, cache)

    riviera_entries = [e for e in log.recent() if e["message"].startswith("ТЦ Ривьера")]
    assert "RGL02: 3→0" in riviera_entries[0]["message"]
    import json
    assert json.loads(riviera_entries[0]["payload"])["changes"] == [{"sku": "RGL02", "before": 3, "after": 0}]


def test_run_once_logs_a_fresh_entry_every_call_without_being_deduplicated(tmp_path, monkeypatch):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    cache = AssortmentCache(str(tmp_path / "assortment.sqlite3"))
    rows_by_store = {store_id: [{"code": "X", "quantity": 1.0}] for store_id in CAMPAIGN_STORES.values()}
    moysklad = FakeMoySklad(rows_by_store=rows_by_store)
    yandex = FakeYandex(offers_by_campaign={c: ["X"] for c in CAMPAIGN_STORES})
    _patched(monkeypatch, moysklad, yandex)

    run_once(FakeSettings(), log, cache)
    run_once(FakeSettings(), log, cache)

    assert len(log.recent(limit=1000)) == len(CAMPAIGN_STORES) * 2
    # second run reused the now-fresh cache instead of refetching the assortment
    assert len(yandex.campaign_offers_calls) == len(CAMPAIGN_STORES)
