from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .yandex_market import YandexMarketClient
from .yandex_market_order_sync import CAMPAIGN_NAMES, CAMPAIGN_STORES
from .yandex_market_sync import YandexMarketSyncLog

MAX_SKUS_PER_REQUEST = 2000
ASSORTMENT_TTL = timedelta(hours=24)


class AssortmentCache:
    """Local cache of each campaign's offerId list (refreshed once a day).

    Yandex Market's full offer listing for a campaign can run to several
    hundred/thousand offers across multiple paginated requests — too slow to
    re-fetch on every 10-minute stock sync tick. Caching it locally also
    means stock is pushed for every offer Yandex actually knows about
    (including sold-out ones, which need an explicit 0), not just the
    products MoySklad's per-store report happens to list that moment (that
    report silently drops products once they hit zero stock at a store).
    """

    def __init__(self, path: str = "data/yandex_market_assortment.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS campaign_offers (
                campaign_id TEXT NOT NULL, offer_id TEXT NOT NULL, refreshed_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, offer_id))"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS stock_state (
                campaign_id TEXT NOT NULL, offer_id TEXT NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY (campaign_id, offer_id))"""
            )

    def offer_ids(self, campaign_id: str) -> list[str]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT offer_id FROM campaign_offers WHERE campaign_id=?", (campaign_id,)).fetchall()
            return [row[0] for row in rows]

    def is_stale(self, campaign_id: str) -> bool:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT MAX(refreshed_at) FROM campaign_offers WHERE campaign_id=?", (campaign_id,)).fetchone()
        if not row or not row[0]:
            return True
        return datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) > ASSORTMENT_TTL

    def replace(self, campaign_id: str, offer_ids: list[str]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM campaign_offers WHERE campaign_id=?", (campaign_id,))
            db.executemany(
                "INSERT INTO campaign_offers(campaign_id, offer_id, refreshed_at) VALUES (?,?,?)",
                [(campaign_id, offer_id, now) for offer_id in offer_ids],
            )

    def last_counts(self, campaign_id: str) -> dict[str, int]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT offer_id, count FROM stock_state WHERE campaign_id=?", (campaign_id,)).fetchall()
            return {offer_id: count for offer_id, count in rows}

    def save_counts(self, campaign_id: str, counts: dict[str, int]) -> None:
        with sqlite3.connect(self.path) as db:
            db.executemany(
                """INSERT INTO stock_state(campaign_id, offer_id, count) VALUES (?,?,?)
                ON CONFLICT(campaign_id, offer_id) DO UPDATE SET count=excluded.count""",
                [(campaign_id, offer_id, count) for offer_id, count in counts.items()],
            )


def _chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def sync_campaign_stock(
    moysklad: MoySkladClient,
    yandex: YandexMarketClient,
    cache: AssortmentCache,
    *,
    campaign_id: str,
    store_id: str,
) -> tuple[int, list[dict[str, Any]] | None]:
    """Push sellable stock to Yandex Market for every offer whose count changed.

    Offers missing from MoySklad's store stock report (typically because
    they've sold down to zero, which drops them from that report entirely)
    count as 0 rather than being skipped, so a sell-out still registers as a
    change and gets pushed. On a campaign's very first sync there's no prior
    state to diff against, so every offer is pushed as a baseline.

    Returns the total offer count in the campaign's assortment and the list
    of changes actually pushed (sku/before/after) — or None on that first
    sync, when nothing has been compared yet.
    """
    if cache.is_stale(campaign_id):
        cache.replace(campaign_id, yandex.campaign_offers(campaign_id))
    offer_ids = cache.offer_ids(campaign_id)

    rows = moysklad.stock_by_store(store_id)
    by_code = {row["code"]: row.get("quantity", 0) for row in rows if row.get("code")}
    new_counts = {offer_id: max(0, round(by_code.get(offer_id, 0))) for offer_id in offer_ids}

    old_counts = cache.last_counts(campaign_id)
    is_first_sync = not old_counts
    changes: list[dict[str, Any]] | None = None
    if is_first_sync:
        to_push = new_counts
    else:
        changed_offer_ids = [offer_id for offer_id, count in new_counts.items() if old_counts.get(offer_id) != count]
        to_push = {offer_id: new_counts[offer_id] for offer_id in changed_offer_ids}
        changes = [{"sku": offer_id, "before": old_counts.get(offer_id), "after": new_counts[offer_id]} for offer_id in changed_offer_ids]

    items = [{"sku": offer_id, "count": count} for offer_id, count in to_push.items()]
    for chunk in _chunks(items, MAX_SKUS_PER_REQUEST):
        yandex.update_stocks(chunk, campaign_id=campaign_id)

    cache.save_counts(campaign_id, new_counts)
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
    yandex = YandexMarketClient(
        base_url=settings.yandex_market_base_url,
        api_key=settings.yandex_market_api_key,
        business_id=settings.yandex_market_business_id,
    )
    try:
        for campaign_id, store_id in CAMPAIGN_STORES.items():
            store_name = CAMPAIGN_NAMES.get(campaign_id, campaign_id)
            try:
                count, changes = sync_campaign_stock(moysklad, yandex, cache, campaign_id=campaign_id, store_id=store_id)
                message = f"{store_name}: остатки обновлены, офферов {count}, {_changes_summary(changes)}"
                log.add("stock_sync", "success", message, None, {"campaign_id": campaign_id, "count": count, "changes": changes})
            except Exception as error:
                log.add("stock_sync", "error", f"{store_name}: ошибка синхронизации остатков: {error}", None, {"campaign_id": campaign_id})
    finally:
        moysklad.close()
        yandex.close()


def worker() -> None:
    settings = Settings.from_env()
    log = YandexMarketSyncLog()
    cache = AssortmentCache()
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, log, cache)
        except Exception as error:
            errors.log_exception("yandex_market_stock_sync_worker", error, context="Ошибка синхронизации остатков с Яндекс.Маркетом")
        time.sleep(600)
