from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .anthropic_client import AnthropicClient
from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .order_assistant import (
    DEFAULT_QUESTION,
    answer_question,
    fetch_marketplace_details,
    find_order_by_label_message,
    read_order_number_from_image,
    resolve_order_by_number,
    resolve_order_from_text,
)
from .ozon_client import OzonClient
from .telegram_client import TelegramClient
from .yandex_market import YandexMarketClient
from .yandex_market_sync import YandexMarketSyncLog

WORKER_TICK_SECONDS = 5


class PendingTelegramMessages:
    """Work queue for inbound Telegram messages the order Q&A assistant
    might need to answer. The webhook handler only ever inserts into this —
    all the slow work (MoySklad/marketplace lookups, the Claude call, the
    Telegram reply — several sequential external API calls) happens in this
    worker loop instead. Doing that work inline in the webhook handler
    blocked every other webhook (Yandex Market, OZON, Shopify) behind it on
    this service's single-request-at-a-time WSGI server, and made Telegram
    itself time out waiting for a response — observed live 2026-09-29,
    mirroring the same reasoning OZON's webhook already follows (see
    ozon_order_sync.PendingPostings)."""

    def __init__(self, path: str = "data/telegram_qa_queue.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS pending_messages (
                id INTEGER PRIMARY KEY, received_at TEXT NOT NULL,
                message_json TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)"""
            )

    def add(self, message: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO pending_messages(received_at, message_json) VALUES (?, ?)",
                (datetime.now(timezone.utc).isoformat(), json.dumps(message, ensure_ascii=False)),
            )

    def pending(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM pending_messages WHERE done=0 ORDER BY id")]

    def mark_done(self, row_id: int) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE pending_messages SET done=1 WHERE id=?", (row_id,))


def enqueue_if_relevant(update: dict[str, Any], *, settings: Settings, queue: PendingTelegramMessages) -> None:
    """Cheap, local-only checks (no network calls) so the webhook handler
    that calls this stays fast regardless of what the message contains —
    the real "is this about an order" resolution happens in the worker."""
    if not settings.anthropic_api_key:
        return
    message = update.get("message")
    if not isinstance(message, dict):
        return
    if bool((message.get("from") or {}).get("is_bot")):
        return
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not settings.telegram_label_chat_id or chat_id != str(settings.telegram_label_chat_id):
        return
    queue.add(message)


def _resolve_order_for_message(message: dict, *, yandex_log: YandexMarketSyncLog, ozon_log: YandexMarketSyncLog, moysklad: MoySkladClient, anthropic: AnthropicClient, telegram: TelegramClient) -> tuple[str | None, str | None, dict | None]:
    """Tries, in order: (1) the message is a reply to a label this service
    sent — exact match by message_id; (2) an order number appears in the
    message's own text/caption, or in the text/caption of whatever it's
    replying to; (3) the message carries a photo (receipt/label screenshot)
    and Claude can read an order number off of it. Each step only costs
    something (a MoySklad lookup, a vision call) once the previous one came
    up empty. Returns (marketplace, external_id, moysklad_order) so the
    caller can also pull the live marketplace order/posting."""
    reply_to = message.get("reply_to_message")
    if isinstance(reply_to, dict) and reply_to.get("message_id"):
        match = find_order_by_label_message(reply_to_message_id=reply_to["message_id"], yandex_log=yandex_log, ozon_log=ozon_log)
        if match is not None:
            marketplace, external_id = match
            order = moysklad.customer_order_by_external_code(external_id) if marketplace == "yandex_market" else moysklad.customer_order_by_name(external_id)
            if order is not None:
                return marketplace, external_id, order

    search_text = str(message.get("text") or message.get("caption") or "")
    if isinstance(reply_to, dict):
        search_text = f"{search_text} {reply_to.get('text') or reply_to.get('caption') or ''}"
    resolved = resolve_order_from_text(text=search_text, moysklad=moysklad)
    if resolved is not None:
        return resolved

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        file_id = photos[-1].get("file_id")  # Telegram orders photo sizes smallest -> largest
        if file_id:
            image_bytes = telegram.download_file(file_id)
            candidate = read_order_number_from_image(client=anthropic, image_bytes=image_bytes)
            if candidate:
                resolved = resolve_order_by_number(candidate=candidate, moysklad=moysklad)
                if resolved is not None:
                    return resolved

    return None, None, None


def _process_one(row: dict[str, Any], *, queue: PendingTelegramMessages, settings: Settings, yandex_log: YandexMarketSyncLog, ozon_log: YandexMarketSyncLog, moysklad: MoySkladClient, anthropic: AnthropicClient, telegram: TelegramClient, yandex: YandexMarketClient | None, ozon: OzonClient | None) -> None:
    message = json.loads(row["message_json"])
    try:
        marketplace, external_id, order = _resolve_order_for_message(message, yandex_log=yandex_log, ozon_log=ozon_log, moysklad=moysklad, anthropic=anthropic, telegram=telegram)
        if order is None:
            return  # nothing about an order in this message — silently drop it
        marketplace_details = fetch_marketplace_details(marketplace=marketplace, external_id=external_id, yandex=yandex, ozon=ozon) if marketplace and external_id else None
        question = str(message.get("text") or message.get("caption") or "").strip() or DEFAULT_QUESTION
        answer = answer_question(client=anthropic, order=order, marketplace=marketplace, question=question, marketplace_details=marketplace_details)
        telegram.send_message(chat_id=settings.telegram_label_chat_id, text=answer, reply_to_message_id=message.get("message_id"))
    finally:
        queue.mark_done(row["id"])


def run_once(settings: Settings, queue: PendingTelegramMessages, errors: ErrorLog) -> None:
    yandex_log = YandexMarketSyncLog()
    ozon_log = YandexMarketSyncLog("data/ozon_sync.sqlite3")
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    anthropic = AnthropicClient(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
    telegram = TelegramClient(bot_token=settings.telegram_bot_token, proxy=settings.telegram_proxy_url)
    yandex = YandexMarketClient(base_url=settings.yandex_market_base_url, api_key=settings.yandex_market_api_key, business_id=settings.yandex_market_business_id) if settings.yandex_market_api_key else None
    ozon = OzonClient(client_id=settings.ozon_client_id, api_key=settings.ozon_api_key) if settings.ozon_api_key else None
    try:
        for row in queue.pending():
            try:
                _process_one(row, queue=queue, settings=settings, yandex_log=yandex_log, ozon_log=ozon_log, moysklad=moysklad, anthropic=anthropic, telegram=telegram, yandex=yandex, ozon=ozon)
            except Exception as error:
                errors.log_exception("telegram_qa_worker", error, context=f"Ошибка ответа на сообщение Telegram id={row['id']}")
                queue.mark_done(row["id"])  # best-effort Q&A — not retried; staff can just ask again
    finally:
        moysklad.close()
        anthropic.close()
        telegram.close()
        if yandex is not None:
            yandex.close()
        if ozon is not None:
            ozon.close()


def worker() -> None:
    settings = Settings.from_env()
    queue = PendingTelegramMessages()
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, queue, errors)
        except Exception as error:
            errors.log_exception("telegram_qa_worker", error, context="Ошибка воркера Telegram Q&A")
        time.sleep(WORKER_TICK_SECONDS)
