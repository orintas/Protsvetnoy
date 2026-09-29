from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

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
from .yandex_gpt_client import YandexGPTClient
from .yandex_market import YandexMarketClient
from .yandex_market_sync import YandexMarketSyncLog

# Telegram's servers can't reach this Russian-hosted VPS on any inbound
# path — the mirror image of the already-documented outbound restriction
# (api.telegram.org unreachable directly, hence the telegram-proxy sidecar).
# A registered webhook consistently failed with "Connection timed out" even
# though the service itself answered a direct external request instantly;
# confirmed live 2026-09-29. So this worker pulls messages via long-polling
# (get_updates) through the same working outbound proxy send_message/
# send_document already use, instead of waiting for Telegram to push.
POLL_TIMEOUT_SECONDS = 25


class UpdateOffset:
    """Persists the last Telegram update_id this service has handled, so a
    worker restart resumes from there instead of Telegram redelivering
    (and this worker reprocessing/re-answering) everything again."""

    def __init__(self, path: str = "data/telegram_qa_offset.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS offset (id INTEGER PRIMARY KEY CHECK (id = 1), update_id INTEGER NOT NULL)")

    def get(self) -> int | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT update_id FROM offset WHERE id = 1").fetchone()
            return row[0] if row else None

    def set(self, update_id: int) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO offset(id, update_id) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET update_id = excluded.update_id", (update_id,))


def _is_relevant(message: dict[str, Any], settings: Settings) -> bool:
    if not isinstance(message, dict):
        return False
    if bool((message.get("from") or {}).get("is_bot")):
        return False
    chat_id = str((message.get("chat") or {}).get("id", ""))
    return bool(settings.telegram_label_chat_id) and chat_id == str(settings.telegram_label_chat_id)


def _resolve_order_for_message(message: dict, *, yandex_log: YandexMarketSyncLog, ozon_log: YandexMarketSyncLog, moysklad: MoySkladClient, llm: YandexGPTClient, telegram: TelegramClient) -> tuple[str | None, str | None, dict | None]:
    """Tries, in order: (1) the message is a reply to a label this service
    sent — exact match by message_id; (2) an order number appears in the
    message's own text/caption, or in the text/caption of whatever it's
    replying to; (3) if the LLM supports image input, the message carries a
    photo (receipt/label screenshot) it can read an order number off of.
    Each step only costs something (a MoySklad lookup, a vision call) once
    the previous one came up empty. Returns (marketplace, external_id,
    moysklad_order) so the caller can also pull the live marketplace
    order/posting."""
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
    if getattr(llm, "supports_images", False) and isinstance(photos, list) and photos:
        file_id = photos[-1].get("file_id")  # Telegram orders photo sizes smallest -> largest
        if file_id:
            image_bytes = telegram.download_file(file_id)
            candidate = read_order_number_from_image(client=llm, image_bytes=image_bytes)
            if candidate:
                resolved = resolve_order_by_number(candidate=candidate, moysklad=moysklad)
                if resolved is not None:
                    return resolved

    return None, None, None


def _answer_message(message: dict[str, Any], *, settings: Settings, yandex_log: YandexMarketSyncLog, ozon_log: YandexMarketSyncLog, moysklad: MoySkladClient, llm: YandexGPTClient, telegram: TelegramClient, yandex: YandexMarketClient | None, ozon: OzonClient | None) -> None:
    marketplace, external_id, order = _resolve_order_for_message(message, yandex_log=yandex_log, ozon_log=ozon_log, moysklad=moysklad, llm=llm, telegram=telegram)
    if order is None:
        return  # nothing about an order in this message — silently drop it
    marketplace_details = fetch_marketplace_details(marketplace=marketplace, external_id=external_id, yandex=yandex, ozon=ozon) if marketplace and external_id else None
    question = str(message.get("text") or message.get("caption") or "").strip() or DEFAULT_QUESTION
    answer = answer_question(client=llm, order=order, marketplace=marketplace, question=question, marketplace_details=marketplace_details)
    telegram.send_message(chat_id=settings.telegram_label_chat_id, text=answer, reply_to_message_id=message.get("message_id"))


def run_once(settings: Settings, offset_store: UpdateOffset, errors: ErrorLog) -> None:
    if not settings.yandexgpt_api_key or not settings.yandexgpt_folder_id:
        return
    yandex_log = YandexMarketSyncLog()
    ozon_log = YandexMarketSyncLog("data/ozon_sync.sqlite3")
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    llm = YandexGPTClient(api_key=settings.yandexgpt_api_key, folder_id=settings.yandexgpt_folder_id, model=settings.yandexgpt_model)
    telegram = TelegramClient(bot_token=settings.telegram_bot_token, proxy=settings.telegram_proxy_url)
    yandex = YandexMarketClient(base_url=settings.yandex_market_base_url, api_key=settings.yandex_market_api_key, business_id=settings.yandex_market_business_id) if settings.yandex_market_api_key else None
    ozon = OzonClient(client_id=settings.ozon_client_id, api_key=settings.ozon_api_key) if settings.ozon_api_key else None
    try:
        last_offset = offset_store.get()
        updates = telegram.get_updates(offset=(last_offset + 1) if last_offset is not None else None, timeout=POLL_TIMEOUT_SECONDS, allowed_updates=["message"])
        for update in updates:
            message = update.get("message")
            try:
                if _is_relevant(message, settings):
                    _answer_message(message, settings=settings, yandex_log=yandex_log, ozon_log=ozon_log, moysklad=moysklad, llm=llm, telegram=telegram, yandex=yandex, ozon=ozon)
            except Exception as error:
                errors.log_exception("telegram_qa_worker", error, context=f"Ошибка ответа на сообщение Telegram update_id={update.get('update_id')}")
            finally:
                offset_store.set(update["update_id"])
    finally:
        moysklad.close()
        llm.close()
        telegram.close()
        if yandex is not None:
            yandex.close()
        if ozon is not None:
            ozon.close()


def worker() -> None:
    settings = Settings.from_env()
    offset_store = UpdateOffset()
    errors = ErrorLog()
    if settings.telegram_bot_token:
        client = TelegramClient(bot_token=settings.telegram_bot_token, proxy=settings.telegram_proxy_url)
        try:
            client.delete_webhook()
        except Exception as error:
            errors.log_exception("telegram_qa_worker", error, context="Не удалось снять регистрацию вебхука Telegram")
        finally:
            client.close()
    while True:
        try:
            run_once(settings, offset_store, errors)
        except Exception as error:
            errors.log_exception("telegram_qa_worker", error, context="Ошибка воркера Telegram Q&A")
            time.sleep(5)  # avoid a tight error loop when get_updates itself keeps failing
