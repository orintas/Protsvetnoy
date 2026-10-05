from datetime import datetime, timedelta, timezone

from sync_service.error_log import ErrorLog
from sync_service.health_check import (
    CheckResult,
    HealthState,
    _heartbeat_check,
    _record,
    check_novicloud_sales,
    check_ozon_webhook,
    check_shift_closer,
    check_shopify_catalog,
    check_shopify_webhook,
    check_yandex_market_stock,
    run_once,
)
from sync_service.shift_closer import ShiftCloseLog
from sync_service.shopify_sync import ShopifySyncLog
from sync_service.sync_log import SyncLog
from sync_service.yandex_market_sync import YandexMarketSyncLog

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)  # 15:00 Moscow, within all business-hours windows


def _iso(dt):
    return dt.isoformat()


def test_heartbeat_check_not_applicable_skips_entirely():
    result = _heartbeat_check("x", "X", applicable=False, entries=[], now=NOW, max_age_minutes=10)
    assert result.applicable is False
    assert result.ok is True


def test_heartbeat_check_fails_when_no_entries_ever():
    result = _heartbeat_check("x", "X", applicable=True, entries=[], now=NOW, max_age_minutes=10)
    assert result.ok is False
    assert "проверок ещё не было" in result.detail


def test_heartbeat_check_fails_when_stale():
    entries = [{"created_at": _iso(NOW - timedelta(minutes=40))}]
    result = _heartbeat_check("x", "X", applicable=True, entries=entries, now=NOW, max_age_minutes=10)
    assert result.ok is False
    assert "40" in result.detail


def test_heartbeat_check_ok_when_recent():
    entries = [{"created_at": _iso(NOW - timedelta(minutes=2))}]
    result = _heartbeat_check("x", "X", applicable=True, entries=entries, now=NOW, max_age_minutes=10)
    assert result.ok is True


def test_check_novicloud_sales_uses_run_kind_only(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    log.add("sale", "success", "unrelated", "ext-1")
    log.add("run", "success", "heartbeat")
    result = check_novicloud_sales(log, datetime.now(timezone.utc))  # real "now" — the entry was just inserted with a real timestamp
    assert result.ok is True


def test_check_yandex_market_stock_not_applicable_outside_hours(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    night = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)  # 04:00 Moscow
    result = check_yandex_market_stock(log, night)
    assert result.applicable is False


def test_check_yandex_market_stock_fails_when_stale_during_hours(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    result = check_yandex_market_stock(log, NOW)  # no entries at all
    assert result.applicable is True
    assert result.ok is False


def test_check_yandex_market_stock_fails_when_every_recent_attempt_errored(tmp_path):
    """The real incident this fixes: Yandex's own API returned 500 for this
    account's offer listing for hours, so every ~10-minute tick produced a
    fresh "stock_sync" row — just all status=error. The old check counted
    any status as a heartbeat and reported healthy the whole time."""
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    log.add("stock_sync", "error", "ТМ Авиапарк: ошибка синхронизации остатков: HTTP 500", None)
    result = check_yandex_market_stock(log, datetime.now(timezone.utc))
    assert result.ok is False


def test_check_shopify_catalog_ok_on_a_recent_run_with_no_changes(tmp_path):
    """A quiet night (nothing changed) legitimately produces zero
    catalog_created/catalog_update/catalog_error rows — only the unconditional
    per-run "catalog_run" summary, which this check now keys off instead."""
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    log.add("catalog_run", "success", "Синхронизация каталога завершена: проверено 50, изменилось 0")
    result = check_shopify_catalog(log, datetime.now(timezone.utc))
    assert result.ok is True


def test_check_shopify_catalog_fails_when_stale(tmp_path):
    log = ShopifySyncLog(str(tmp_path / "shopify.sqlite3"))
    result = check_shopify_catalog(log, NOW)  # no entries at all
    assert result.ok is False


def test_check_shift_closer_ok_when_ran_today_or_yesterday(tmp_path):
    log = ShiftCloseLog(str(tmp_path / "shift.sqlite3"))
    log.set_last_run_date("2026-09-28")  # yesterday relative to NOW (Moscow date 2026-09-29)
    result = check_shift_closer(log, NOW)
    assert result.ok is True


def test_check_shift_closer_fails_when_missed_more_than_a_day(tmp_path):
    log = ShiftCloseLog(str(tmp_path / "shift.sqlite3"))
    log.set_last_run_date("2026-09-26")
    result = check_shift_closer(log, NOW)
    assert result.ok is False
    assert "2026-09-26" in result.detail


def test_check_shift_closer_fails_when_never_run(tmp_path):
    log = ShiftCloseLog(str(tmp_path / "shift.sqlite3"))
    result = check_shift_closer(log, NOW)
    assert result.ok is False


class FakeShopifyClient:
    def __init__(self, webhooks=None, raise_error=None):
        self._webhooks = webhooks or []
        self._raise = raise_error

    def webhooks(self, *, topic=None):
        if self._raise:
            raise self._raise
        return self._webhooks


def test_check_shopify_webhook_none_client_is_not_applicable():
    result = check_shopify_webhook(None)
    assert result.applicable is False


def test_check_shopify_webhook_ok_when_registered():
    client = FakeShopifyClient(webhooks=[{"address": "https://protsvetnoy.us/api/shopify/webhook/orders", "topic": "orders/create"}])
    result = check_shopify_webhook(client)
    assert result.ok is True


def test_check_shopify_webhook_fails_when_missing():
    client = FakeShopifyClient(webhooks=[])
    result = check_shopify_webhook(client)
    assert result.ok is False
    assert "не найдена" in result.detail


def test_check_shopify_webhook_fails_on_api_error():
    client = FakeShopifyClient(raise_error=RuntimeError("boom"))
    result = check_shopify_webhook(client)
    assert result.ok is False
    assert "boom" in result.detail


class FakeOzonClient:
    def __init__(self, urls=None, raise_error=None):
        self._urls = urls or []
        self._raise = raise_error

    def notification_list(self):
        if self._raise:
            raise self._raise
        return {"urls": self._urls}


def test_check_ozon_webhook_none_client_is_not_applicable():
    assert check_ozon_webhook(None).applicable is False


def test_check_ozon_webhook_ok_when_green_and_enabled():
    client = FakeOzonClient(urls=[{"url": "https://protsvetnoy.us/api/ozon/webhook/notifications", "enable": True, "availability_status": "GREEN"}])
    result = check_ozon_webhook(client)
    assert result.ok is True


def test_check_ozon_webhook_fails_when_disabled():
    client = FakeOzonClient(urls=[{"url": "https://protsvetnoy.us/api/ozon/webhook/notifications", "enable": False, "availability_status": "GREEN"}])
    result = check_ozon_webhook(client)
    assert result.ok is False
    assert "отключена" in result.detail


def test_check_ozon_webhook_fails_when_red():
    client = FakeOzonClient(urls=[{"url": "https://protsvetnoy.us/api/ozon/webhook/notifications", "enable": True, "availability_status": "RED", "reason_details": "too many failures"}])
    result = check_ozon_webhook(client)
    assert result.ok is False
    assert "too many failures" in result.detail


def test_check_ozon_webhook_fails_when_not_registered():
    client = FakeOzonClient(urls=[])
    result = check_ozon_webhook(client)
    assert result.ok is False


def test_record_logs_once_on_transition_to_failure_not_every_tick(tmp_path):
    state = HealthState(str(tmp_path / "state.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    failing = CheckResult("x", "X", applicable=True, ok=False, detail="X сломан")

    _record(state, errors, failing)
    _record(state, errors, failing)  # same failure again — must not re-log
    _record(state, errors, failing)

    assert len(errors.recent()) == 1
    assert errors.recent()[0]["message"] == "X сломан"


def test_record_logs_recovery_once_when_it_comes_back(tmp_path):
    state = HealthState(str(tmp_path / "state.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    failing = CheckResult("x", "X", applicable=True, ok=False, detail="X сломан")
    healthy = CheckResult("x", "X", applicable=True, ok=True, detail="")

    _record(state, errors, failing)
    _record(state, errors, healthy)
    _record(state, errors, healthy)  # already healthy — must not re-log "recovered" again

    entries = errors.recent()
    assert len(entries) == 2
    assert entries[0]["message"] == "X: снова работает"  # most recent first
    assert entries[0]["level"] == "info"  # a recovery notice, not a real failure
    assert entries[1]["message"] == "X сломан"
    assert entries[1]["level"] == "error"


def test_record_skips_not_applicable_results(tmp_path):
    state = HealthState(str(tmp_path / "state.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    result = CheckResult("x", "X", applicable=False, ok=True, detail="")

    _record(state, errors, result)

    assert errors.recent() == []
    assert state.was_ok("x") is None  # never recorded either


def test_run_once_returns_a_result_per_check(tmp_path, monkeypatch):
    import sync_service.health_check as mod

    monkeypatch.setattr(mod, "SyncLog", lambda: SyncLog(str(tmp_path / "sync.sqlite3")))
    monkeypatch.setattr(mod, "YandexMarketSyncLog", lambda: YandexMarketSyncLog(str(tmp_path / "ym.sqlite3")))
    monkeypatch.setattr(mod, "ShopifySyncLog", lambda: ShopifySyncLog(str(tmp_path / "shopify.sqlite3")))
    monkeypatch.setattr(mod, "ShiftCloseLog", lambda: ShiftCloseLog(str(tmp_path / "shift.sqlite3")))

    class FakeSettings:
        shopify_shop_domain = ""
        shopify_access_token = ""
        shopify_api_version = "2026-07"
        ozon_client_id = ""
        ozon_api_key = ""

    state = HealthState(str(tmp_path / "state.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))

    results = run_once(FakeSettings(), state, errors, now=NOW)

    names = {r.name for r in results}
    assert names == {"novicloud_sales", "yandex_market_stock", "shopify_catalog", "shopify_stock", "shift_closer", "shopify_webhook", "ozon_webhook"}
    # no Shopify/OZON credentials configured in FakeSettings — those two checks are simply not applicable
    assert next(r for r in results if r.name == "shopify_webhook").applicable is False
    assert next(r for r in results if r.name == "ozon_webhook").applicable is False
