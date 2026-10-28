from __future__ import annotations

import re
import sqlite3
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

# How long after a long-running worker's own process starts a failure still
# counts as "probably just the deploy restart settling" rather than a real
# problem. Confirmed live 2026-10-16: every worker container restarts at
# once on each deploy, and the first network call or two afterward can hit
# a transient "Temporary failure in name resolution" or "Connection reset
# by peer" while Docker's networking for the fresh container finishes
# settling — not a real failure, and not worth treating as one every single
# deploy. A worker hitting the very same error well after this window has
# passed is a real problem and still logged at the normal "error" level.
STARTUP_GRACE_SECONDS = 30.0


def level_for_startup_grace(started_at: float, *, grace_seconds: float = STARTUP_GRACE_SECONDS) -> str:
    """`started_at` is a time.monotonic() timestamp taken once, right when
    the worker's own loop began (after any deliberate startup delay)."""
    return "info" if time.monotonic() - started_at < grace_seconds else "error"


# How long a network outage may last before it counts as a real failure.
# Confirmed live 2026-10-25..28: the Telegram VLESS tunnel drops every
# connection every 6 hours sharp (xx:59:20 UTC) plus the odd SSL EOF /
# handshake timeout / Telegram 502 in between, and recovers on its own
# within seconds to a minute — every one of those used to land in the
# journal as an "error" from both Telegram workers at once (54% of all
# rows), while no message was ever lost (the update offset is persisted).
TRANSIENT_OUTAGE_SECONDS = 300.0


def is_transient_network_error(error: BaseException) -> bool:
    """Connection-level failure (reset, timeout, SSL EOF) or an HTTP 5xx
    from the remote end — the kind that self-heals without anyone acting."""
    if isinstance(error, httpx.TransportError):
        return True
    return re.search(r"HTTP 5\d\d\b", str(error)) is not None


class OutageTracker:
    """Folds a run of transient network failures in a polling loop into at
    most three journal rows instead of one per failed iteration: an "info"
    row when the outage starts, one "error" row if it is still ongoing after
    `threshold_seconds`, and an "info" recovery row if that error was
    logged. Anything that isn't a transient network error is logged as a
    normal error every time, exactly as before."""

    def __init__(self, errors: ErrorLog, source: str, *, threshold_seconds: float = TRANSIENT_OUTAGE_SECONDS) -> None:
        self._errors = errors
        self._source = source
        self._threshold_seconds = threshold_seconds
        self._failing_since: float | None = None
        self._escalated = False

    def failed(self, error: BaseException, *, context: str) -> None:
        if not is_transient_network_error(error):
            self._errors.log_exception(self._source, error, context=context)
            return
        now = time.monotonic()
        if self._failing_since is None:
            self._failing_since = now
            self._errors.log_exception(self._source, error, context=f"{context} (временный сбой сети, повторяем)", level="info")
        elif not self._escalated and now - self._failing_since >= self._threshold_seconds:
            self._escalated = True
            minutes = (now - self._failing_since) / 60
            self._errors.log_exception(self._source, error, context=f"{context} (сеть недоступна уже {minutes:.0f} мин)")

    def succeeded(self) -> None:
        if self._escalated and self._failing_since is not None:
            minutes = (time.monotonic() - self._failing_since) / 60
            self._errors.add(self._source, f"Связь восстановилась после {minutes:.0f} мин сбоя", level="info")
        self._failing_since = None
        self._escalated = False


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
            columns = {row[1] for row in db.execute("PRAGMA table_info(error_log)")}
            if "level" not in columns:
                db.execute("ALTER TABLE error_log ADD COLUMN level TEXT")

    def add(self, source: str, message: str, details: str = "", *, level: str = "error") -> None:
        """`level` distinguishes a real unresolved failure ("error", the
        default) from an informational recovery notice ("info", e.g. a
        health check reporting something that was failing now works again)
        — both share this table so the UI can show them together, but a
        recovery notice must not look identical to a real failure (badge
        color alone used to be the only differentiator)."""
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO error_log(created_at,source,message,details,read_at,level) VALUES(?,?,?,?,NULL,?)",
                (datetime.now(timezone.utc).isoformat(), source, message, details, level),
            )

    def log_exception(self, source: str, error: BaseException, context: str = "", *, level: str = "error") -> None:
        """Record an exception with its full traceback as the detail payload."""
        details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        message = f"{context}: {error}" if context else str(error)
        self.add(source, message, details, level=level)

    def recent(self, limit: int = 2000, *, days: int = 3) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM error_log WHERE created_at >= ? ORDER BY id DESC LIMIT ?", (cutoff, limit))]

    def unread_count(self) -> int:
        """Excludes "info"-level rows — a recovery/benign notice shouldn't
        trip the "Есть ошибки" indicator the way an actual unresolved
        failure does."""
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT COUNT(*) FROM error_log WHERE read_at IS NULL AND (level IS NULL OR level != 'info')").fetchone()
            return int(row[0])

    def mark_all_read(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE error_log SET read_at=? WHERE read_at IS NULL",
                (datetime.now(timezone.utc).isoformat(),),
            )
