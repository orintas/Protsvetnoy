from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .error_log import ErrorLog
from .novicloud import NovicloudClient


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("dane", "sprzedaz", "sales", "items", "rows"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


class SyncLog:
    def __init__(self, path: str = "data/sync.sqlite3") -> None:
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


def _sale_summary(item: dict[str, Any]) -> str:
    doc = item.get("nr_dok") or item.get("nr_systemowy") or ""
    amount = item.get("brutto")
    shop = ((item.get("sklep") or {}).get("id")) if isinstance(item.get("sklep"), dict) else None
    when = item.get("data") or ""
    parts = []
    if doc:
        parts.append(f"чек {doc}")
    if amount is not None:
        parts.append(f"на сумму {amount} PLN")
    if shop is not None:
        parts.append(f"магазин №{shop}")
    if when:
        parts.append(f"от {when}")
    return ", ".join(parts) if parts else "Продажа"


def run_once(settings: Settings, log: SyncLog) -> None:
    client = NovicloudClient(
        base_url=settings.novicloud_base_url,
        version=settings.novicloud_api_version,
        account=settings.novicloud_account,
        password=settings.novicloud_password,
    )
    since = datetime.now() - timedelta(minutes=10)
    try:
        payload = client.sales(date_from=since)
        for item in _items(payload):
            external_id = str(item.get("id") or item.get("numer") or item.get("nr") or "")
            operation_type = str(item.get("typ") or item.get("typ_sprzedazy") or item.get("type") or "")
            if operation_type == "60":
                summary = _sale_summary(item)
                log.add("return", "dry-run", f"Возврат найден ({summary}); документ в МойСклад не создавался — тестовый режим, синхронизация ещё не включена", external_id, item)
            elif operation_type in ("21", ""):
                summary = _sale_summary(item)
                log.add("sale", "dry-run", f"Продажа найдена ({summary}); документ в МойСклад не создавался — тестовый режим, синхронизация ещё не включена", external_id, item)
        log.add("run", "success", "Проверка завершена в тестовом режиме")
    except Exception as error:
        log.add("run", "error", f"Ошибка проверки: {error}")
        raise
    finally:
        client.close()


def worker() -> None:
    settings = Settings.from_env()
    log = SyncLog()
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, log)
        except Exception as error:
            errors.log_exception("novicloud_sync_worker", error, context="Ошибка проверки продаж/возвратов Novicloud")
        time.sleep(300)
