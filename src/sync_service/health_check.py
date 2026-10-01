from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .error_log import ErrorLog
from .ozon_client import OzonClient
from .shift_closer import ShiftCloseLog
from .shopify_client import ShopifyClient
from .shopify_sync import ShopifySyncLog
from .sync_log import SyncLog
from .yandex_market_sync import YandexMarketSyncLog

MOSCOW = ZoneInfo("Europe/Moscow")

# How often the worker itself ticks — independent of each check's own
# staleness threshold below.
WORKER_TICK_SECONDS = 1800


class HealthState:
    """Remembers each check's last known result, so a failure is logged to
    the shared error journal once on the healthy→unhealthy transition —
    not every tick for as long as the same issue is ongoing — and a
    recovery is logged once too, so "it's fixed now" is visible without
    having to keep checking by hand."""

    def __init__(self, path: str = "data/health_check.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS check_state (name TEXT PRIMARY KEY, ok INTEGER NOT NULL, updated_at TEXT NOT NULL)")

    def was_ok(self, name: str) -> bool | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT ok FROM check_state WHERE name=?", (name,)).fetchone()
            return bool(row[0]) if row else None

    def set_ok(self, name: str, ok: bool) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO check_state(name, ok, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET ok=excluded.ok, updated_at=excluded.updated_at",
                (name, int(ok), datetime.now(timezone.utc).isoformat()),
            )


@dataclass
class CheckResult:
    name: str
    label: str
    applicable: bool
    ok: bool
    detail: str


def _minutes_since(entries: list[dict[str, Any]], now: datetime) -> float | None:
    if not entries:
        return None
    latest = max(datetime.fromisoformat(e["created_at"]) for e in entries)
    return (now.astimezone(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds() / 60


def _heartbeat_check(name: str, label: str, *, applicable: bool, entries: list[dict[str, Any]], now: datetime, max_age_minutes: float) -> CheckResult:
    if not applicable:
        return CheckResult(name, label, applicable=False, ok=True, detail="")
    age = _minutes_since(entries, now)
    if age is None:
        return CheckResult(name, label, applicable=True, ok=False, detail=f"{label}: проверок ещё не было")
    if age > max_age_minutes:
        return CheckResult(name, label, applicable=True, ok=False, detail=f"{label}: последняя проверка была {age:.0f} мин назад (ожидалось не больше {max_age_minutes:.0f})")
    return CheckResult(name, label, applicable=True, ok=True, detail="")


def check_novicloud_sales(log: SyncLog, now: datetime) -> CheckResult:
    """Heartbeat fires every ~15 min tick regardless of hour (see
    novicloud_retail_sync.run_once's unconditional "run" summary)."""
    entries = [e for e in log.recent(limit=20) if e["kind"] == "run"]
    return _heartbeat_check("novicloud_sales", "Продажи Novicloud", applicable=True, entries=entries, now=now, max_age_minutes=25)


def check_yandex_market_stock(log: YandexMarketSyncLog, now: datetime) -> CheckResult:
    """Only counts successful ticks as the heartbeat — a sync that's firing
    every 10 minutes but failing every single time (confirmed live
    2026-10-01: Yandex's own API returned 500 for this account's offer
    listing for hours) still produces fresh "stock_sync" log rows, so
    counting any status here would have reported healthy the whole time."""
    now_moscow = now.astimezone(MOSCOW)
    applicable = 9 <= now_moscow.hour < 22
    entries = [e for e in log.recent(limit=40) if e["kind"] == "stock_sync" and e["status"] == "success"]
    return _heartbeat_check("yandex_market_stock", "Остатки Яндекс.Маркет", applicable=applicable, entries=entries, now=now, max_age_minutes=18)


def check_shopify_catalog(log: ShopifySyncLog, now: datetime) -> CheckResult:
    """Keys off the nightly "catalog_run" summary (always logged as success
    once the full pass completes, see shopify_sync.sync_catalog) rather than
    individual catalog_created/catalog_update/catalog_error rows — those can
    legitimately be zero on a quiet night (nothing changed) or, the gap this
    replaces, all failures, which used to still count as "it ran"."""
    entries = [e for e in log.recent(limit=40) if e["kind"] == "catalog_run"]
    return _heartbeat_check("shopify_catalog", "Каталог Shopify", applicable=True, entries=entries, now=now, max_age_minutes=26 * 60)


def check_shopify_stock(log: ShopifySyncLog, now: datetime) -> CheckResult:
    now_moscow = now.astimezone(MOSCOW)
    applicable = 9 <= now_moscow.hour < 22
    entries = [e for e in log.recent(limit=40) if e["kind"] == "stock_run"]
    return _heartbeat_check("shopify_stock", "Остатки Shopify", applicable=applicable, entries=entries, now=now, max_age_minutes=40)


def check_shift_closer(log: ShiftCloseLog, now: datetime) -> CheckResult:
    name, label = "shift_closer", "Закрытие смен"
    now_moscow = now.astimezone(MOSCOW)
    last_run_date = log.last_run_date()
    if last_run_date is None:
        return CheckResult(name, label, applicable=True, ok=False, detail=f"{label}: проверок ещё не было")
    try:
        last_run = datetime.fromisoformat(last_run_date).date()
    except ValueError:
        return CheckResult(name, label, applicable=True, ok=False, detail=f"{label}: не удалось разобрать дату последнего запуска «{last_run_date}»")
    ok = last_run >= now_moscow.date() - timedelta(days=1)
    detail = "" if ok else f"{label}: последний успешный запуск был {last_run_date}, это больше суток назад"
    return CheckResult(name, label, applicable=True, ok=ok, detail=detail)


def check_shopify_webhook(client: ShopifyClient | None) -> CheckResult:
    name, label = "shopify_webhook", "Вебхук заказов Shopify"
    if client is None:
        return CheckResult(name, label, applicable=False, ok=True, detail="")
    try:
        webhooks = client.webhooks(topic="orders/create")
    except Exception as error:
        return CheckResult(name, label, applicable=True, ok=False, detail=f"{label}: не удалось проверить — {error}")
    alive = any("/api/shopify/webhook/orders" in (w.get("address") or "") for w in webhooks)
    detail = "" if alive else f"{label}: подписка orders/create не найдена в Shopify — заказы не будут попадать в МойСклад автоматически"
    return CheckResult(name, label, applicable=True, ok=alive, detail=detail)


def check_ozon_webhook(client: OzonClient | None) -> CheckResult:
    name, label = "ozon_webhook", "Вебхук OZON"
    if client is None:
        return CheckResult(name, label, applicable=False, ok=True, detail="")
    try:
        result = client.notification_list()
    except Exception as error:
        return CheckResult(name, label, applicable=True, ok=False, detail=f"{label}: не удалось проверить — {error}")
    matching = [u for u in result.get("urls", []) if "/api/ozon/webhook/notifications" in (u.get("url") or "")]
    if not matching:
        return CheckResult(name, label, applicable=True, ok=False, detail=f"{label}: подписка не найдена в OZON — этикетки не будут отправляться автоматически")
    entry = matching[0]
    if not entry.get("enable"):
        return CheckResult(name, label, applicable=True, ok=False, detail=f"{label}: подписка отключена в OZON")
    if entry.get("availability_status") == "RED":
        return CheckResult(name, label, applicable=True, ok=False, detail=f"{label}: статус RED ({entry.get('reason_details') or 'причина не указана'})")
    return CheckResult(name, label, applicable=True, ok=True, detail="")


def _record(state: HealthState, errors: ErrorLog, result: CheckResult) -> None:
    if not result.applicable:
        return
    was_ok = state.was_ok(result.name)
    if not result.ok and was_ok is not False:
        errors.add("health_check", result.detail or f"{result.label}: не работает")
    elif result.ok and was_ok is False:
        errors.add("health_check", f"{result.label}: снова работает")
    state.set_ok(result.name, result.ok)


def run_once(settings: Settings, state: HealthState, errors: ErrorLog, *, now: datetime | None = None) -> list[CheckResult]:
    now = now or datetime.now(timezone.utc)
    results = [
        check_novicloud_sales(SyncLog(), now),
        check_yandex_market_stock(YandexMarketSyncLog(), now),
        check_shopify_catalog(ShopifySyncLog(), now),
        check_shopify_stock(ShopifySyncLog(), now),
        check_shift_closer(ShiftCloseLog(), now),
    ]

    shopify_client = None
    if settings.shopify_shop_domain and settings.shopify_access_token:
        shopify_client = ShopifyClient(shop_domain=settings.shopify_shop_domain, access_token=settings.shopify_access_token, api_version=settings.shopify_api_version)
    try:
        results.append(check_shopify_webhook(shopify_client))
    finally:
        if shopify_client is not None:
            shopify_client.close()

    ozon_client = None
    if settings.ozon_client_id and settings.ozon_api_key:
        ozon_client = OzonClient(client_id=settings.ozon_client_id, api_key=settings.ozon_api_key)
    try:
        results.append(check_ozon_webhook(ozon_client))
    finally:
        if ozon_client is not None:
            ozon_client.close()

    for result in results:
        _record(state, errors, result)
    return results


def worker() -> None:
    settings = Settings.from_env()
    state = HealthState()
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, state, errors)
        except Exception as error:
            errors.log_exception("health_check_worker", error, context="Ошибка воркера проверки состояния синхронизаций")
        time.sleep(WORKER_TICK_SECONDS)
