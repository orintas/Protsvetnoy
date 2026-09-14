from sync_service.yandex_market_order_sync import CAMPAIGN_STORES
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
        self.campaign_offers_calls = []
        self.closed = False

    def campaign_offers(self, campaign_id):
        self.campaign_offers_calls.append(campaign_id)
        return self.offers_by_campaign.get(campaign_id, [])

    def update_stocks(self, items, *, campaign_id):
        if campaign_id in self.fail_campaigns:
            raise RuntimeError("boom yandex")
        self.calls.append((campaign_id, items))

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

    count = sync_campaign_stock(moysklad, yandex, cache, campaign_id="149179260", store_id="store-1")

    assert count == 2
    assert yandex.campaign_offers_calls == ["149179260"]
    assert yandex.calls == [("149179260", [{"sku": "RGL02", "count": 3}, {"sku": "SOLD_OUT_SKU", "count": 0}])]


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
