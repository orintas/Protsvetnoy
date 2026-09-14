from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class YandexMarketSyncLog:
    """Shared journal for everything Yandex Market related, independent from Novicloud's sync_log."""

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
