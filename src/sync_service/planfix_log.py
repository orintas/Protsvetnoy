from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class PlanFixSyncLog:
    """Journal for everything PlanFix-related only — not a catch-all for the
    whole project. Other services keep their own logs; this one stays scoped
    to operations that actually touch PlanFix (incoming webhooks, contact
    transfers, future PlanFix-driven projects)."""

    def __init__(self, path: str = "data/planfix_sync.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                external_id TEXT, status TEXT NOT NULL, message TEXT NOT NULL,
                payload TEXT NOT NULL)"""
            )

    def add(self, kind: str, status: str, message: str, external_id: str | None = None, payload: Any = None) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO sync_log(created_at,kind,external_id,status,message,payload) VALUES(?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), kind, external_id, status, message, json.dumps(payload, ensure_ascii=False, default=str)),
            )

    def recent(self, limit: int = 2000, *, days: int = 3) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM sync_log WHERE created_at >= ? ORDER BY id DESC LIMIT ?", (cutoff, limit))]

    def search(self, query: str, limit: int = 200) -> list[dict[str, Any]]:
        like = f"%{query}%"
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM sync_log WHERE external_id LIKE ? OR message LIKE ? ORDER BY id DESC LIMIT ?",
                    (like, like, limit),
                )
            ]
