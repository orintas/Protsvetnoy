from sync_service.yandex_market_order_sync import CAMPAIGN_STORES
from sync_service.yandex_market_stock_sync import sync_campaign_stock
from sync_service.yandex_market_sync import YandexMarketSyncLog
from sync_service.yandex_market_stock_sync import run_once


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
    def __init__(self, fail_campaigns=None):
        self.fail_campaigns = fail_campaigns or set()
        self.calls = []
        self.closed = False

    def update_stocks(self, items, *, campaign_id):
        if campaign_id in self.fail_campaigns:
            raise RuntimeError("boom yandex")
        self.calls.append((campaign_id, items))

    def close(self):
        self.closed = True


def test_sync_campaign_stock_builds_items_from_code_and_quantity():
    moysklad = FakeMoySklad(rows_by_store={"store-1": [
        {"code": "RGL02", "quantity": 3.0},
        {"code": "BLU15", "quantity": 0.0},
        {"code": "", "quantity": 5.0},
    ]})
    yandex = FakeYandex()
    count = sync_campaign_stock(moysklad, yandex, campaign_id="149179260", store_id="store-1")
    assert count == 2
    assert yandex.calls == [("149179260", [{"sku": "RGL02", "count": 3}, {"sku": "BLU15", "count": 0}])]


def test_sync_campaign_stock_clamps_negative_quantity_to_zero():
    moysklad = FakeMoySklad(rows_by_store={"store-1": [{"code": "RGL02", "quantity": -2.0}]})
    yandex = FakeYandex()
    sync_campaign_stock(moysklad, yandex, campaign_id="149179260", store_id="store-1")
    assert yandex.calls == [("149179260", [{"sku": "RGL02", "count": 0}])]


def test_run_once_syncs_every_known_campaign(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    rows_by_store = {store_id: [{"code": "X", "quantity": 1.0}] for store_id in CAMPAIGN_STORES.values()}
    moysklad = FakeMoySklad(rows_by_store=rows_by_store)
    yandex = FakeYandex()

    import sync_service.yandex_market_stock_sync as mod
    original_moysklad_client = mod.MoySkladClient
    original_yandex_client = mod.YandexMarketClient
    mod.MoySkladClient = lambda **kwargs: moysklad
    mod.YandexMarketClient = lambda **kwargs: yandex
    try:
        run_once(FakeSettings(), log)
    finally:
        mod.MoySkladClient = original_moysklad_client
        mod.YandexMarketClient = original_yandex_client

    synced_campaigns = {call[0] for call in yandex.calls}
    assert synced_campaigns == set(CAMPAIGN_STORES)
    entries = log.recent()
    assert len(entries) == len(CAMPAIGN_STORES)
    assert all(e["kind"] == "stock_sync" and e["status"] == "success" for e in entries)
    assert moysklad.closed and yandex.closed


def test_run_once_logs_error_for_one_campaign_but_continues_others(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    failing_store = CAMPAIGN_STORES["149179260"]
    rows_by_store = {store_id: [{"code": "X", "quantity": 1.0}] for store_id in CAMPAIGN_STORES.values()}
    moysklad = FakeMoySklad(rows_by_store=rows_by_store, fail_stores={failing_store})
    yandex = FakeYandex()

    import sync_service.yandex_market_stock_sync as mod
    original_moysklad_client = mod.MoySkladClient
    original_yandex_client = mod.YandexMarketClient
    mod.MoySkladClient = lambda **kwargs: moysklad
    mod.YandexMarketClient = lambda **kwargs: yandex
    try:
        run_once(FakeSettings(), log)
    finally:
        mod.MoySkladClient = original_moysklad_client
        mod.YandexMarketClient = original_yandex_client

    synced_campaigns = {call[0] for call in yandex.calls}
    assert synced_campaigns == set(CAMPAIGN_STORES) - {"149179260"}
    entries = log.recent()
    error_entries = [e for e in entries if e["status"] == "error"]
    assert len(error_entries) == 1
    assert "149179260" in error_entries[0]["message"]


def test_run_once_logs_a_fresh_entry_every_call_without_being_deduplicated(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    rows_by_store = {store_id: [{"code": "X", "quantity": 1.0}] for store_id in CAMPAIGN_STORES.values()}
    moysklad = FakeMoySklad(rows_by_store=rows_by_store)
    yandex = FakeYandex()

    import sync_service.yandex_market_stock_sync as mod
    original_moysklad_client = mod.MoySkladClient
    original_yandex_client = mod.YandexMarketClient
    mod.MoySkladClient = lambda **kwargs: moysklad
    mod.YandexMarketClient = lambda **kwargs: yandex
    try:
        run_once(FakeSettings(), log)
        run_once(FakeSettings(), log)
    finally:
        mod.MoySkladClient = original_moysklad_client
        mod.YandexMarketClient = original_yandex_client

    assert len(log.recent(limit=1000)) == len(CAMPAIGN_STORES) * 2
