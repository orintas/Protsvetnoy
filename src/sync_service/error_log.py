from __future__ import annotations

import sqlite3
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ErrorLog:
    """Unified, service-wide log of errors from API calls and sync workers.

    Separate from the per-integration SyncLog/YandexMarketSyncLog tables
    (which record every check, success or failure, for a human-readable
    audit trail): this one exists purely to surface failures, with a full
    traceback kept for diagnosis and a read/unread flag so the UI can show
    an "unread errors" indicator.
    """

    def __init__(self, path: str = "data/error_log.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS error_log (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL,
                message TEXT NOT NULL, details TEXT NOT NULL, read_at TEXT)"""
            )

    def add(self, source: str, message: str, details: str = "") -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO error_log(created_at,source,message,details,read_at) VALUES(?,?,?,?,NULL)",
                (datetime.now(timezone.utc).isoformat(), source, message, details),
            )

    def log_exception(self, source: str, error: BaseException, context: str = "") -> None:
        """Record an exception with its full traceback as the detail payload."""
        details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        message = f"{context}: {error}" if context else str(error)
        self.add(source, message, details)

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM error_log ORDER BY id DESC LIMIT ?", (limit,))]

    def unread_count(self) -> int:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT COUNT(*) FROM error_log WHERE read_at IS NULL").fetchone()
            return int(row[0])

    def mark_all_read(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE error_log SET read_at=? WHERE read_at IS NULL",
                (datetime.now(timezone.utc).isoformat(),),
            )
