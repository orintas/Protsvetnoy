from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .error_log import ErrorLog
from .yandex_market import YandexMarketClient


class YandexMarketSyncLog:
    """Separate journal for Yandex Market order sync, independent from Novicloud's sync_log."""

    def __init__(self, path: str = "data/yandex_market_sync.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                external_id TEXT, status TEXT NOT NULL, message TEXT NOT NULL,
                payload TEXT NOT NULL)"""
            )
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS sync_log_external_operation
                ON sync_log(kind, external_id)
                WHERE external_id IS NOT NULL AND external_id <> ''"""
            )

    def add(self, kind: str, status: str, message: str, external_id: str | None = None, payload: Any = None) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR IGNORE INTO sync_log(created_at,kind,external_id,status,message,payload) VALUES(?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), kind, external_id, status, message, json.dumps(payload, ensure_ascii=False, default=str)),
            )

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))]


def run_once(settings: Settings, log: YandexMarketSyncLog) -> None:
    client = YandexMarketClient(
        base_url=settings.yandex_market_base_url,
        api_key=settings.yandex_market_api_key,
        business_id=settings.yandex_market_business_id,
    )
    since = datetime.now() - timedelta(days=1)
    try:
        orders = client.all_orders(update_date_from=since, update_date_to=datetime.now())
        for order in orders:
            external_id = str(order.get("orderId") or "")
            status = str(order.get("status") or "")
            items = order.get("items") or []
            summary = f"Заказ {external_id}: статус {status}, позиций {len(items)}"
            if status in ("CANCELLED", "RETURNED", "PARTIALLY_RETURNED"):
                log.add("return", "dry-run", f"{summary} (отмена/возврат); документ не создавался", external_id, order)
            else:
                log.add("order", "dry-run", f"{summary}; документ не создавался", external_id, order)
        log.add("run", "success", f"Проверка завершена в тестовом режиме, заказов найдено: {len(orders)}")
    except Exception as error:
        log.add("run", "error", f"Ошибка проверки: {error}")
        raise
    finally:
        client.close()


def worker() -> None:
    settings = Settings.from_env()
    log = YandexMarketSyncLog()
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, log)
        except Exception as error:
            errors.log_exception("yandex_market_sync_worker", error, context="Ошибка проверки заказов Яндекс.Маркета")
        time.sleep(300)
