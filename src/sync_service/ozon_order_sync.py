from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .error_log import ErrorLog
from .http import ApiError
from .label_caption import build_caption, format_items_plain
from .moysklad import MoySkladClient
from .ozon_client import OzonClient
from .telegram_client import TelegramClient
from .yandex_market_sync import YandexMarketSyncLog

# Only these 4 rFBS mall warehouses get their labels sent to Telegram — same
# ids as ozon_stock_sync.OZON_WAREHOUSES minus "Склад Цветной" (Основной
# склад), which is plain FBS, not rFBS. Per explicit request: the main
# warehouse's postings are left alone entirely (not even enqueued).
RFBS_WAREHOUSE_IDS = {
    1020001195674000,  # Экспресс_ТЦ_Авиапарк
    1020005000377375,  # ТЦ Саларис
    1020001195607000,  # ТЦ Ривьера
    1020000096205000,  # Экспресс_ТЦ Мега Химки
}

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

# 3 postings abandoned by the description_updated migration bug (2026-09-17/18,
# fixed just below) before ever shipping or getting a label sent — requeued
# once, the first time the fixed code runs against the old table. Safe to
# remove this once the 2026-09-18 incident is confirmed resolved.
STUCK_BY_DESCRIPTION_UPDATED_BUG = ["56444838-0115-1", "07223445-0177-1", "76135049-0093-1"]


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
                description_updated INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
                last_status TEXT)"""
            )
            # CREATE TABLE IF NOT EXISTS is a no-op against a table that already
            # exists from an earlier deploy — it does not add new columns, so a
            # column added after the table's first release needs an explicit
            # migration here or every read of it raises KeyError.
            existing_columns = {row[1] for row in db.execute("PRAGMA table_info(pending_postings)")}
            if "description_updated" not in existing_columns:
                db.execute("ALTER TABLE pending_postings ADD COLUMN description_updated INTEGER NOT NULL DEFAULT 0")
                db.executemany(
                    "UPDATE pending_postings SET done=0, attempts=0 WHERE posting_number=? AND done=1",
                    [(posting_number,) for posting_number in STUCK_BY_DESCRIPTION_UPDATED_BUG],
                )
            if "last_status" not in existing_columns:
                db.execute("ALTER TABLE pending_postings ADD COLUMN last_status TEXT")

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

    def mark_description_updated(self, posting_number: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE pending_postings SET description_updated=1 WHERE posting_number=?", (posting_number,))

    def increment_attempts(self, posting_number: str) -> int:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE pending_postings SET attempts=attempts+1 WHERE posting_number=?", (posting_number,))
            row = db.execute("SELECT attempts FROM pending_postings WHERE posting_number=?", (posting_number,)).fetchone()
            return row[0] if row else 0

    def mark_done(self, posting_number: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE pending_postings SET done=1 WHERE posting_number=?", (posting_number,))

    def update_last_status(self, posting_number: str, status: str) -> None:
        """Records what the last tick actually observed (an OZON status, or
        'lookup_failed' when posting_details itself errored) — purely
        diagnostic, so a give-up message says what it was stuck on instead
        of nothing at all."""
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE pending_postings SET last_status=? WHERE posting_number=?", (status, posting_number))


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
        try:
            warehouse_id = int(payload.get("warehouse_id") or 0)
        except (TypeError, ValueError):
            warehouse_id = 0
        if posting_number and warehouse_id in RFBS_WAREHOUSE_IDS and queue.add(posting_number):
            log.add("webhook", "success", f"Новое отправление OZON {posting_number}", posting_number, payload)
    return {"result": True}


def _log_error(log: YandexMarketSyncLog, errors: ErrorLog, kind: str, message: str, posting_number: str) -> None:
    """Every OZON-pipeline failure goes through here so it always shows up
    in both places — the OZON-specific log (for context alongside the rest
    of that posting's history) and the shared error journal (so a real
    failure is visible without having to know to look at the OZON tab).
    A prior version logged some failures to only one of the two, which is
    exactly the kind of gap this centralizes against."""
    log.add(kind, "error", message, posting_number)
    errors.add("ozon_order_sync", message)


def _send_label(
    *,
    posting_number: str,
    details: dict[str, Any],
    ozon: OzonClient,
    telegram: TelegramClient | None,
    telegram_chat_id: str,
    log: YandexMarketSyncLog,
    errors: ErrorLog,
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
    _log_error(log, errors, "label_sent", f"Отправление {posting_number}: Telegram не настроен, этикетка не отправлена", posting_number)
    return True  # nothing left to retry — the config problem won't fix itself on a re-attempt


def _update_moysklad_description(posting_number: str, details: dict[str, Any], moysklad: MoySkladClient, queue: PendingPostings, log: YandexMarketSyncLog) -> None:
    """Prepends the ordered SKUs (one per line, with quantity) to whatever
    description OZON's own MoySklad integration already wrote — that other
    integration creates the customerorder itself (named after the posting
    number), we only ever touch its description field."""
    order = moysklad.customer_order_by_name(posting_number)
    if order is None:
        return  # the other integration hasn't created the document yet — retry next tick
    items = [{"sku": p.get("offer_id"), "count": p.get("quantity", 1)} for p in details.get("products", [])]
    existing = order.get("description") or ""
    description = f"{format_items_plain(items)}\n{existing}" if existing else format_items_plain(items)
    moysklad.update_customer_order_description(str(order["id"]), description, previous_description=existing or None)
    log.add("description_updated", "success", f"Отправление {posting_number}: список товаров добавлен в описание заказа МойСклад", posting_number)
    queue.mark_description_updated(posting_number)


def _process_one(row: dict[str, Any], queue: PendingPostings, ozon: OzonClient, moysklad: MoySkladClient, telegram: TelegramClient | None, telegram_chat_id: str, log: YandexMarketSyncLog, errors: ErrorLog) -> None:
    posting_number = row["posting_number"]
    attempts = queue.increment_attempts(posting_number)
    if attempts > MAX_ATTEMPTS:
        last_status = row.get("last_status") or "неизвестен (posting_details ни разу не ответил за это время)"
        message = f"Отправление {posting_number}: этикетка не появилась за {MAX_ATTEMPTS} попыток, дальше не пробуем. Последний известный статус: {last_status}"
        _log_error(log, errors, "order_error", message, posting_number)
        queue.mark_done(posting_number)
        return

    details = ozon.posting_details(posting_number)
    if details is None:
        queue.update_last_status(posting_number, "lookup_failed")
        return  # transient/lookup failure — try again next tick

    status = details.get("status")
    queue.update_last_status(posting_number, status or "(пусто)")
    if status in TERMINAL_STATUSES:
        log.add("order_cancelled", "success", f"Отправление {posting_number}: статус «{status}», этикетка не нужна", posting_number)
        queue.mark_done(posting_number)
        return

    if not row["description_updated"]:
        try:
            _update_moysklad_description(posting_number, details, moysklad, queue, log)
        except Exception as error:
            _log_error(log, errors, "order_pipeline_error", f"Отправление {posting_number}: не удалось обновить описание заказа в МойСклад: {error}", posting_number)

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
                _log_error(log, errors, "order_pipeline_error", f"Отправление {posting_number}: не удалось подтвердить упаковку: {error}", posting_number)
                # not marked shipped — the SHIP_FROM_STATUS branch retries it next tick
        return  # label needs ~45-60s after shipping — try it on a later tick

    if status in LABEL_READY_STATUSES or already_shipped:
        if _send_label(posting_number=posting_number, details=details, ozon=ozon, telegram=telegram, telegram_chat_id=telegram_chat_id, log=log, errors=errors):
            queue.mark_done(posting_number)


def run_once(settings: Settings, queue: PendingPostings, log: YandexMarketSyncLog, errors: ErrorLog) -> None:
    ozon = OzonClient(client_id=settings.ozon_client_id, api_key=settings.ozon_api_key)
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    telegram = TelegramClient(bot_token=settings.telegram_bot_token, proxy=settings.telegram_proxy_url) if settings.telegram_bot_token else None
    try:
        for row in queue.pending():
            try:
                _process_one(row, queue, ozon, moysklad, telegram, settings.telegram_label_chat_id, log, errors)
            except Exception as error:
                _log_error(log, errors, "order_pipeline_error", f"Отправление {row['posting_number']}: ошибка обработки: {error}", row["posting_number"])
    finally:
        ozon.close()
        moysklad.close()
        if telegram is not None:
            telegram.close()


def worker() -> None:
    settings = Settings.from_env()
    queue = PendingPostings()
    log = YandexMarketSyncLog("data/ozon_sync.sqlite3")
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, queue, log, errors)
        except Exception as error:
            errors.log_exception("ozon_order_sync_worker", error, context="Ошибка синхронизации заказов/этикеток OZON")
        time.sleep(WORKER_TICK_SECONDS)
