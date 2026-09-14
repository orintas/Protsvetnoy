from __future__ import annotations

import time
from typing import Any

from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .yandex_market import YandexMarketClient
from .yandex_market_order_sync import CAMPAIGN_STORES
from .yandex_market_sync import YandexMarketSyncLog

MAX_SKUS_PER_REQUEST = 2000


def _chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def sync_campaign_stock(moysklad: MoySkladClient, yandex: YandexMarketClient, *, campaign_id: str, store_id: str) -> int:
    """Push sellable stock for one campaign's store to Yandex Market. Returns the sku count pushed."""
    rows = moysklad.stock_by_store(store_id)
    items = [
        {"sku": row["code"], "count": max(0, round(row.get("quantity", 0)))}
        for row in rows
        if row.get("code")
    ]
    for chunk in _chunks(items, MAX_SKUS_PER_REQUEST):
        yandex.update_stocks(chunk, campaign_id=campaign_id)
    return len(items)


def run_once(settings: Settings, log: YandexMarketSyncLog) -> None:
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    yandex = YandexMarketClient(
        base_url=settings.yandex_market_base_url,
        api_key=settings.yandex_market_api_key,
        business_id=settings.yandex_market_business_id,
    )
    try:
        for campaign_id, store_id in CAMPAIGN_STORES.items():
            try:
                count = sync_campaign_stock(moysklad, yandex, campaign_id=campaign_id, store_id=store_id)
                log.add("stock_sync", "success", f"Кампания {campaign_id}: остатки обновлены, товаров {count}", None, {"campaign_id": campaign_id, "count": count})
            except Exception as error:
                log.add("stock_sync", "error", f"Кампания {campaign_id}: ошибка синхронизации остатков: {error}", None, {"campaign_id": campaign_id})
    finally:
        moysklad.close()
        yandex.close()


def worker() -> None:
    settings = Settings.from_env()
    log = YandexMarketSyncLog()
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, log)
        except Exception as error:
            errors.log_exception("yandex_market_stock_sync_worker", error, context="Ошибка синхронизации остатков с Яндекс.Маркетом")
        time.sleep(600)
