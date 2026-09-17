from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .error_log import ErrorLog
from .http import ApiError
from .label_caption import build_caption
from .ozon_client import OzonClient
from .telegram_client import TelegramClient
from .yandex_market_sync import YandexMarketSyncLog

# Statuses at/after which a posting's label is expected to be downloadable —
# confirmed live: real postings sitting in "awaiting_deliver" already had
# "label_download" in their available_actions. Ship only applies to postings
# still in "awaiting_packaging"; anything already past that (whichever way it
# got there) skips straight to trying the label.
LABEL_READY_STATUSES = {"awaiting_deliver", "delivering", "driver_pickup", "delivered"}
SHIP_FROM_STATUS = "awaiting_packaging"
TERMINAL_STATUSES = {"cancelled", "not_accepted"}

MAX_ATTEMPTS = 40  # ~20 minutes at the worker's 30s tick — well past OZON's documented 45-60s label delay
WORKER_TICK_SECONDS = 30


class PendingPostings:
    """Work queue for postings seen via TYPE_NEW_POSTING but not yet fully
    handled (shipped + label sent). The webhook handler only ever inserts
    into this — all the slow work (ship, wait for the label, send it) happens
    in the worker loop, since OZON auto-suspends notifications if a handler
    takes over 5 seconds."""

    def __init__(self, path: str = "data/ozon_pending_postings.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS pending_postings (
                posting_number TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, shipped INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0)"""
            )

    def add(self, posting_number: str) -> bool:
        """Returns True if this is a newly-seen posting (False if already queued/done)."""
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO pending_postings(posting_number, first_seen_at) VALUES (?, ?)",
                (posting_number, datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount > 0

    def pending(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM pending_postings WHERE done=0 ORDER BY first_seen_at")]

    def mark_shipped(self, posting_number: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE pending_postings SET shipped=1 WHERE posting_number=?", (posting_number,))

    def increment_attempts(self, posting_number: str) -> int:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE pending_postings SET attempts=attempts+1 WHERE posting_number=?", (posting_number,))
            row = db.execute("SELECT attempts FROM pending_postings WHERE posting_number=?", (posting_number,)).fetchone()
            return row[0] if row else 0

    def mark_done(self, posting_number: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE pending_postings SET done=1 WHERE posting_number=?", (posting_number,))


def handle_webhook_notification(payload: dict[str, Any], queue: PendingPostings, log: YandexMarketSyncLog) -> dict[str, Any]:
    """Processes one OZON push notification. Must stay fast (<5s total,
    including whatever wraps this in the web handler) — OZON auto-suspends
    all notifications after enough slow/failed responses.

    Returns the JSON body the webhook endpoint should answer with.
    """
    message_type = payload.get("message_type")
    if message_type == "TYPE_PING":
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {"version": "1.0.0", "name": "Varvikas sync service", "time": now}
    if message_type == "TYPE_NEW_POSTING":
        posting_number = str(payload.get("posting_number") or "")
        if posting_number and queue.add(posting_number):
            log.add("webhook", "success", f"Новое отправление OZON {posting_number}", posting_number, payload)
    return {"result": True}


def _send_label(
    *,
    posting_number: str,
    details: dict[str, Any],
    ozon: OzonClient,
    telegram: TelegramClient | None,
    telegram_chat_id: str,
    log: YandexMarketSyncLog,
) -> bool:
    """Returns whether the label was actually fetched+sent this call — False
    for "not ready yet" (caller should retry later), raises for anything else."""
    try:
        label_pdf = ozon.package_label([posting_number])
    except ApiError as error:
        if "ready" in str(error).lower():
            return False
        raise

    store_name = str((details.get("delivery_method") or {}).get("warehouse") or "OZON")
    items = [{"sku": p.get("offer_id"), "count": p.get("quantity", 1)} for p in details.get("products", [])]
    if telegram is not None and telegram_chat_id:
        telegram.send_document(
            chat_id=telegram_chat_id,
            document=label_pdf,
            filename=f"{posting_number}.pdf",
            caption=build_caption(marketplace="OZON", store_name=store_name, order_label=f"Заказ №{posting_number}", items=items),
            parse_mode="HTML",
        )
        log.add("label_sent", "success", f"Отправление {posting_number}: этикетка отправлена в Telegram", posting_number)
        return True
    log.add("label_sent", "error", f"Отправление {posting_number}: Telegram не настроен, этикетка не отправлена", posting_number)
    return True  # nothing left to retry — the config problem won't fix itself on a re-attempt


def _process_one(row: dict[str, Any], queue: PendingPostings, ozon: OzonClient, telegram: TelegramClient | None, telegram_chat_id: str, log: YandexMarketSyncLog) -> None:
    posting_number = row["posting_number"]
    attempts = queue.increment_attempts(posting_number)
    if attempts > MAX_ATTEMPTS:
        log.add("order_error", "error", f"Отправление {posting_number}: этикетка не появилась за {MAX_ATTEMPTS} попыток, дальше не пробуем", posting_number)
        queue.mark_done(posting_number)
        return

    details = ozon.posting_details(posting_number)
    if details is None:
        return  # transient/lookup failure — try again next tick

    status = details.get("status")
    if status in TERMINAL_STATUSES:
        log.add("order_cancelled", "success", f"Отправление {posting_number}: статус «{status}», этикетка не нужна", posting_number)
        queue.mark_done(posting_number)
        return

    already_shipped = bool(row["shipped"])
    if status == SHIP_FROM_STATUS and not already_shipped:
        try:
            ozon.ship_posting(posting_number, details.get("products", []))
            log.add("order_created", "success", f"Отправление {posting_number}: упаковка подтверждена", posting_number)
            queue.mark_shipped(posting_number)
        except ApiError as error:
            if "already" in str(error).lower():
                queue.mark_shipped(posting_number)  # a prior attempt (by us or someone else) already succeeded
            else:
                log.add("order_pipeline_error", "error", f"Отправление {posting_number}: не удалось подтвердить упаковку: {error}", posting_number)
                # not marked shipped — the SHIP_FROM_STATUS branch retries it next tick
        return  # label needs ~45-60s after shipping — try it on a later tick

    if status in LABEL_READY_STATUSES or already_shipped:
        if _send_label(posting_number=posting_number, details=details, ozon=ozon, telegram=telegram, telegram_chat_id=telegram_chat_id, log=log):
            queue.mark_done(posting_number)


def run_once(settings: Settings, queue: PendingPostings, log: YandexMarketSyncLog) -> None:
    ozon = OzonClient(client_id=settings.ozon_client_id, api_key=settings.ozon_api_key)
    telegram = TelegramClient(bot_token=settings.telegram_bot_token, proxy=settings.telegram_proxy_url) if settings.telegram_bot_token else None
    try:
        for row in queue.pending():
            try:
                _process_one(row, queue, ozon, telegram, settings.telegram_label_chat_id, log)
            except Exception as error:
                log.add("order_pipeline_error", "error", f"Отправление {row['posting_number']}: ошибка обработки: {error}", row["posting_number"])
    finally:
        ozon.close()
        if telegram is not None:
            telegram.close()


def worker() -> None:
    settings = Settings.from_env()
    queue = PendingPostings()
    log = YandexMarketSyncLog("data/ozon_sync.sqlite3")
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, queue, log)
        except Exception as error:
            errors.log_exception("ozon_order_sync_worker", error, context="Ошибка синхронизации заказов/этикеток OZON")
        time.sleep(WORKER_TICK_SECONDS)
