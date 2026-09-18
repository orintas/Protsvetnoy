from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import json

RETENTION_DAYS = 180


class ChangeLog:
    """Service-wide before/after audit trail for every write this service
    makes to an external API (MoySklad, Yandex Market, OZON, Shopify,
    Telegram) — separate from the per-integration sync logs, which record
    every check (success or failure) but not necessarily the old value a
    field held before a write replaced it.

    Rows older than RETENTION_DAYS are purged on open, mirroring the other
    sqlite-backed logs in this service (each is instantiated once per
    worker process, so this runs roughly once per deploy/restart, not per
    write — cheap enough with the created_at index for a low-volume log
    like this one).
    """

    def __init__(self, path: str = "data/change_log.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                service TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
                action TEXT NOT NULL, before TEXT, after TEXT, summary TEXT)"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_changes_created_at ON changes(created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_changes_entity_id ON changes(entity_id)")
            cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
            db.execute("DELETE FROM changes WHERE created_at < ?", (cutoff,))

    def add(
        self,
        *,
        service: str,
        entity_type: str,
        entity_id: str | None,
        action: str,
        before: Any = None,
        after: Any = None,
        summary: str | None = None,
    ) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                """INSERT INTO changes(created_at, service, entity_type, entity_id, action, before, after, summary)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    service,
                    entity_type,
                    entity_id,
                    action,
                    json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                    json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                    summary,
                ),
            )

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,))]

    def search(self, query: str, limit: int = 200) -> list[dict[str, Any]]:
        like = f"%{query}%"
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    """SELECT * FROM changes WHERE entity_id LIKE ? OR summary LIKE ? OR service LIKE ? OR entity_type LIKE ?
                    ORDER BY id DESC LIMIT ?""",
                    (like, like, like, like, limit),
                )
            ]


_instance: ChangeLog | None = None


def _get() -> ChangeLog:
    global _instance
    if _instance is None:
        _instance = ChangeLog()
    return _instance


def record(
    *,
    service: str,
    entity_type: str,
    entity_id: str | None,
    action: str,
    before: Any = None,
    after: Any = None,
    summary: str | None = None,
) -> None:
    """Best-effort: a logging failure must never break the actual API write
    it's recording, so this swallows its own errors."""
    try:
        _get().add(service=service, entity_type=entity_type, entity_id=entity_id, action=action, before=before, after=after, summary=summary)
    except Exception:
        pass
