from __future__ import annotations

import re

from .anthropic_client import AnthropicClient
from .moysklad import MoySkladClient
from .yandex_market_sync import YandexMarketSyncLog

MARKETPLACE_NAMES = {"yandex_market": "Яндекс.Маркет", "ozon": "OZON"}

DEFAULT_QUESTION = "Какой сейчас статус этого заказа?"

# The assistant only ever answers from the order data it's given and never
# claims to act — cancelling (or any other order-state change) stays a human
# action taken directly in MoySklad/the marketplace's own cabinet, per the
# explicit "propose but never execute on its own" constraint this feature was
# scoped under.
SYSTEM_PROMPT = """Ты — помощник склада цветочного магазина «Varvikas | Цветной» в рабочем чате Telegram, где сборщики читают этикетки заказов и иногда спрашивают про них — ответом на сообщение, номером заказа или фото чека/этикетки.

Тебе передают данные одного конкретного заказа и вопрос сотрудника о нём. Отвечай кратко, по-русски, только на основе переданных данных о заказе. Если в данных нет ответа на вопрос — так и скажи, не придумывай.

Если сотрудник просит отменить заказ или явно выражает намерение его отменить — ты не можешь отменить заказ сам, у тебя нет такой возможности. Объясни это и скажи, что отмену нужно сделать вручную: в МойСклад и (если нужно, чтобы покупатель тоже узнал) в личном кабинете соответствующей площадки. Никогда не пиши, что заказ отменён или что ты его отменяешь.

Пиши обычным текстом, без markdown-разметки и заголовков — это сообщение в чат."""

IMAGE_ORDER_NUMBER_PROMPT = """На фото — чек, этикетка или экран кассы склада цветочного магазина. Найди номер заказа: для Яндекс.Маркета это длинное число (обычно 8-12 цифр), для OZON — номер отправления вида «12345-0001-1». Ответь ТОЛЬКО этим номером, без единого лишнего слова, пробела или знака препинания. Если номер заказа на фото не виден или не удаётся разобрать, ответь ровно одним словом: НЕТ."""

# Loose order-number shapes worth trying against MoySklad — deliberately
# permissive (the real check is "does MoySklad actually have an order under
# this id", via resolve_order_by_number), so overmatching here is harmless.
_ORDER_NUMBER_PATTERNS = (
    re.compile(r"\d{1,6}-\d{3,6}-\d{1,3}"),  # OZON posting number, e.g. 12345-0001-1
    re.compile(r"\d{8,12}"),  # Yandex Market order id
)


def extract_order_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for pattern in _ORDER_NUMBER_PATTERNS:
        for match in pattern.finditer(text or ""):
            value = match.group(0)
            if value not in candidates:
                candidates.append(value)
    return candidates


def find_order_by_label_message(*, reply_to_message_id: int, yandex_log: YandexMarketSyncLog, ozon_log: YandexMarketSyncLog) -> tuple[str, str] | None:
    """Resolves an inbound reply to the label message it's under: which
    marketplace and which order/posting number. None if the message being
    replied to isn't a label this service ever sent."""
    order_id = yandex_log.find_by_label_message_id(reply_to_message_id)
    if order_id is not None:
        return "yandex_market", order_id
    posting_number = ozon_log.find_by_label_message_id(reply_to_message_id)
    if posting_number is not None:
        return "ozon", posting_number
    return None


def resolve_order_by_number(*, candidate: str, moysklad: MoySkladClient) -> tuple[str, dict] | None:
    """Tries a candidate first as a Yandex Market order id (externalCode),
    then as an OZON posting number (document name) — the two conventions
    this service's own order-creation code uses (see
    yandex_market_order_sync.process_new_order and the OZON pipeline's
    MoySklad lookups). Whichever matches tells us the marketplace; neither
    matching just means this candidate isn't a real order, not an error."""
    order = moysklad.customer_order_by_external_code(candidate)
    if order is not None:
        return "yandex_market", order
    order = moysklad.customer_order_by_name(candidate)
    if order is not None:
        return "ozon", order
    return None


def resolve_order_from_text(*, text: str, moysklad: MoySkladClient) -> tuple[str, dict] | None:
    for candidate in extract_order_candidates(text):
        match = resolve_order_by_number(candidate=candidate, moysklad=moysklad)
        if match is not None:
            return match
    return None


def read_order_number_from_image(*, client: AnthropicClient, image_bytes: bytes, image_media_type: str = "image/jpeg") -> str | None:
    text = client.complete(system=IMAGE_ORDER_NUMBER_PROMPT, user_message="Какой номер заказа на этом фото?", image_bytes=image_bytes, image_media_type=image_media_type, max_tokens=32)
    value = text.strip()
    if not value or value.upper() == "НЕТ":
        return None
    return value


def _order_context_text(*, order: dict, marketplace: str | None) -> str:
    state_name = (order.get("state") or {}).get("name", "неизвестен")
    header = f"Площадка: {MARKETPLACE_NAMES.get(marketplace, marketplace)}\n" if marketplace else ""
    return (
        f"{header}"
        f"Номер документа в МойСклад: {order.get('name')}\n"
        f"Статус в МойСклад: {state_name}\n"
        f"Состав заказа:\n{order.get('description') or '(нет данных)'}"
    )


def answer_question(*, client: AnthropicClient, order: dict, marketplace: str | None, question: str) -> str:
    context = _order_context_text(order=order, marketplace=marketplace)
    return client.complete(system=SYSTEM_PROMPT, user_message=f"Данные о заказе:\n{context}\n\nВопрос от сотрудника склада:\n{question}")
