from __future__ import annotations

import time
from typing import Any

from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .ozon_client import OzonClient
from .yandex_market_stock_sync import AssortmentCache
from .yandex_market_sync import YandexMarketSyncLog

# OZON FBS/rFBS warehouse_id (GET /v2/warehouse/list) -> MoySklad store id.
# The four mall warehouses are the exact same physical stores Yandex Market
# already syncs (see yandex_market_order_sync.CAMPAIGN_STORES); "Склад
# Цветной" is OZON's name for the general "Основной склад" — confirmed live
# against both APIs, not just matched by name.
OZON_WAREHOUSES: dict[int, str] = {
    1020001195674000: "497d98c2-7e21-11ee-0a80-0e2a000dc91f",  # Экспресс_ТЦ_Авиапарк
    1020005000377375: "0caf123c-6e09-11f0-0a80-00c900243b0d",  # ТЦ Саларис
    1020001195607000: "e1222420-16f2-11ed-0a80-010b002a9d1c",  # ТЦ Ривьера
    1020000096205000: "dc9b7c0e-a66d-11eb-0a80-09b9002a05ad",  # Экспресс_ТЦ Мега Химки
    23709754228000: "689a000c-a14e-11e2-9030-001b21d91495",  # Склад Цветной
}

OZON_WAREHOUSE_NAMES: dict[int, str] = {
    1020001195674000: "ТМ Авиапарк",
    1020005000377375: "ТЦ Саларис",
    1020001195607000: "ТЦ Ривьера",
    1020000096205000: "ТЦ МЕГА Химки",
    23709754228000: "Основной склад",
}


def sync_warehouse_stock(
    moysklad: MoySkladClient,
    ozon: OzonClient,
    cache: AssortmentCache,
    *,
    warehouse_id: int,
    store_id: str,
) -> tuple[int, list[dict[str, Any]] | None]:
    """Push sellable stock to OZON for every offer whose count changed.

    Structurally identical to yandex_market_stock_sync.sync_campaign_stock
    (same diff-only push, same "missing from MoySklad's report = 0" rule) —
    kept as a separate function since OZON's assortment cache is keyed by
    warehouse_id (int) rather than a campaign_id string, but the two are
    deliberately parallel so both marketplaces behave the same way.
    """
    cache_key = str(warehouse_id)
    if cache.is_stale(cache_key):
        cache.replace(cache_key, ozon.product_offer_ids())
    offer_ids = cache.offer_ids(cache_key)

    rows = moysklad.stock_by_store(store_id)
    by_code = {row["code"]: row.get("quantity", 0) for row in rows if row.get("code")}
    new_counts = {offer_id: max(0, round(by_code.get(offer_id, 0))) for offer_id in offer_ids}

    old_counts = cache.last_counts(cache_key)
    is_first_sync = not old_counts
    changes: list[dict[str, Any]] | None = None
    if is_first_sync:
        to_push = new_counts
    else:
        changed_offer_ids = [offer_id for offer_id, count in new_counts.items() if old_counts.get(offer_id) != count]
        to_push = {offer_id: new_counts[offer_id] for offer_id in changed_offer_ids}
        changes = [{"sku": offer_id, "before": old_counts.get(offer_id), "after": new_counts[offer_id]} for offer_id in changed_offer_ids]

    if to_push:
        items = [{"sku": offer_id, "count": count} for offer_id, count in to_push.items()]
        ozon.update_stocks(items, warehouse_id=warehouse_id)

    cache.save_counts(cache_key, new_counts)
    return len(offer_ids), changes


CHANGES_PREVIEW_LIMIT = 8


def _changes_summary(changes: list[dict[str, Any]] | None) -> str:
    if changes is None:
        return "первая синхронизация"
    if not changes:
        return "изменений нет"
    preview = ", ".join(f"{c['sku']}: {c['before'] if c['before'] is not None else '—'}→{c['after']}" for c in changes[:CHANGES_PREVIEW_LIMIT])
    extra = len(changes) - CHANGES_PREVIEW_LIMIT
    return f"изменилось {len(changes)}: {preview}" + (f" и ещё {extra}" if extra > 0 else "")


def run_once(settings: Settings, log: YandexMarketSyncLog, cache: AssortmentCache) -> None:
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    ozon = OzonClient(client_id=settings.ozon_client_id, api_key=settings.ozon_api_key)
    try:
        for warehouse_id, store_id in OZON_WAREHOUSES.items():
            warehouse_name = OZON_WAREHOUSE_NAMES.get(warehouse_id, str(warehouse_id))
            try:
                count, changes = sync_warehouse_stock(moysklad, ozon, cache, warehouse_id=warehouse_id, store_id=store_id)
                message = f"OZON {warehouse_name}: остатки обновлены, офферов {count}, {_changes_summary(changes)}"
                log.add("ozon_stock_sync", "success", message, None, {"warehouse_id": warehouse_id, "count": count, "changes": changes})
            except Exception as error:
                log.add("ozon_stock_sync", "error", f"OZON {warehouse_name}: ошибка синхронизации остатков: {error}", None, {"warehouse_id": warehouse_id})
    finally:
        moysklad.close()
        ozon.close()


def worker() -> None:
    settings = Settings.from_env()
    # Reuses YandexMarketSyncLog's class/schema (kept generic enough for this)
    # but its own file — OZON entries don't belong mixed into the Yandex
    # Market log the web UI reads from.
    log = YandexMarketSyncLog("data/ozon_sync.sqlite3")
    cache = AssortmentCache("data/ozon_assortment.sqlite3")
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, log, cache)
        except Exception as error:
            errors.log_exception("ozon_stock_sync_worker", error, context="Ошибка синхронизации остатков с OZON")
        time.sleep(1800)
