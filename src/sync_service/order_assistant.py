from __future__ import annotations

import re
from typing import Any

from .anthropic_client import AnthropicClient
from .moysklad import MoySkladClient
from .ozon_client import OzonClient
from .yandex_market import YandexMarketClient
from .yandex_market_sync import YandexMarketSyncLog

MARKETPLACE_NAMES = {"yandex_market": "Яндекс.Маркет", "ozon": "OZON"}

DEFAULT_QUESTION = "Какой сейчас статус этого заказа?"

# The assistant can read anything in MoySklad/OZON/Yandex Market about the
# order (including courier contact details) but never claims to act —
# cancelling or any other order-state change stays a human action taken
# directly in MoySklad/the marketplace's own cabinet, per the explicit
# "propose but never execute on its own" constraint this feature was scoped
# under.
SYSTEM_PROMPT = """Ты — помощник склада цветочного магазина «Varvikas | Цветной» в рабочем чате Telegram, где сборщики читают этикетки заказов и иногда спрашивают про них — ответом на сообщение, номером заказа или фото чека/этикетки.

Тебе передают данные одного конкретного заказа (из МойСклад и, если есть, с площадки — Яндекс.Маркет или OZON, включая данные о курьере) и вопрос сотрудника о нём. Отвечай кратко, по-русски, только на основе переданных данных. Если в данных нет ответа на вопрос — так и скажи, не придумывай.

Ты не можешь ничего изменить в заказе — ни отменить, ни поменять статус, ни связаться с курьером или покупателем. Если сотрудник просит об этом — объясни, что сам ты этого сделать не можешь, и что нужно сделать вручную (в МойСклад и/или в личном кабинете площадки). Никогда не пиши, что что-то сделал или меняешь.

Пиши обычным текстом, без markdown-разметки и заголовков — это сообщение в чат."""

IMAGE_ORDER_NUMBER_PROMPT = """На фото — чек, этикетка или экран кассы склада цветочного магазина. Найди номер заказа: для Яндекс.Маркета это длинное число (обычно 8-12 цифр), для OZON — номер отправления вида «12345-0001-1». Ответь ТОЛЬКО этим номером, без единого лишнего слова, пробела или знака препинания. Если номер заказа на фото не виден или не удаётся разобрать, ответь ровно одним словом: НЕТ."""

# Loose order-number shapes worth trying against MoySklad — deliberately
# permissive (the real check is "does MoySklad actually have an order under
# this id", via resolve_order_by_number), so overmatching here is harmless.
# Bounds are intentionally generous: real OZON posting numbers were observed
# with prefixes from 5 to 10 digits (e.g. "0113798402-0282-1"), and a tight
# upper bound previously made the regex match a truncated tail instead of
# the full number (confirmed live 2026-09-30 — 13 of 14 real posting
# numbers extracted wrong). \b word boundaries ensure a match always starts
# at the actual beginning of a digit run rather than silently sliding into
# the middle of a longer one when it doesn't fit the bound.
_ORDER_NUMBER_PATTERNS = (
    re.compile(r"\b\d{4,15}-\d{2,8}-\d{1,4}\b"),  # OZON posting number, e.g. 87792534-0050-1
    re.compile(r"\b\d{8,12}\b"),  # Yandex Market order id
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


def resolve_order_by_number(*, candidate: str, moysklad: MoySkladClient) -> tuple[str, str, dict] | None:
    """Tries a candidate first as a Yandex Market order id (externalCode),
    then as an OZON posting number (document name) — the two conventions
    this service's own order-creation code uses (see
    yandex_market_order_sync.process_new_order and the OZON pipeline's
    MoySklad lookups). Whichever matches tells us the marketplace; neither
    matching just means this candidate isn't a real order, not an error."""
    order = moysklad.customer_order_by_external_code(candidate)
    if order is not None:
        return "yandex_market", candidate, order
    order = moysklad.customer_order_by_name(candidate)
    if order is not None:
        return "ozon", candidate, order
    return None


def resolve_order_from_text(*, text: str, moysklad: MoySkladClient) -> tuple[str, str, dict] | None:
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


def fetch_marketplace_details(*, marketplace: str, external_id: str, yandex: YandexMarketClient | None, ozon: OzonClient | None) -> dict[str, Any] | None:
    """The live order/posting from the marketplace itself — richer than what
    MoySklad holds (delivery status, courier name/phone/vehicle). Best
    effort: any API error (or the relevant client not being configured)
    just means the answer falls back to MoySklad-only context instead of
    failing the whole question."""
    try:
        if marketplace == "yandex_market" and yandex is not None:
            return yandex.order_by_id(int(external_id))
        if marketplace == "ozon" and ozon is not None:
            return ozon.posting_details(external_id)
    except Exception:
        return None
    return None


def _courier_line(marketplace: str, details: dict[str, Any]) -> str | None:
    if marketplace == "yandex_market":
        courier = (((details.get("delivery") or {}).get("transfer")) or {}).get("courier") or {}
        if not courier:
            return None
        vehicle = " ".join(part for part in (courier.get("vehicleDescription"), courier.get("vehicleNumber")) if part)
        return f"Курьер: {courier.get('fullName', '?')}, тел. {courier.get('phone', '?')}" + (f", авто {vehicle}" if vehicle else "")
    if marketplace == "ozon":
        courier = details.get("courier") or {}
        if not courier:
            return None
        vehicle = " ".join(part for part in (courier.get("car_model"), courier.get("car_number")) if part)
        return f"Курьер: {courier.get('name', '?')}, тел. {courier.get('phone', '?')}" + (f", авто {vehicle}" if vehicle else "")
    return None


def _marketplace_context_lines(*, marketplace: str | None, details: dict[str, Any] | None) -> list[str]:
    if not marketplace or not details:
        return []
    lines: list[str] = []
    if marketplace == "yandex_market":
        lines.append(f"Статус на Яндекс.Маркете: {details.get('status')}/{details.get('substatus')}")
    elif marketplace == "ozon":
        lines.append(f"Статус на OZON: {details.get('status')} ({details.get('provider_status') or details.get('substatus') or '?'})")
    courier_line = _courier_line(marketplace, details)
    if courier_line:
        lines.append(courier_line)
    return lines


def _order_context_text(*, order: dict, marketplace: str | None, marketplace_details: dict[str, Any] | None = None) -> str:
    state_name = (order.get("state") or {}).get("name", "неизвестен")
    header = f"Площадка: {MARKETPLACE_NAMES.get(marketplace, marketplace)}\n" if marketplace else ""
    lines = [
        f"{header}"
        f"Номер документа в МойСклад: {order.get('name')}\n"
        f"Статус в МойСклад: {state_name}\n"
        f"Состав заказа:\n{order.get('description') or '(нет данных)'}"
    ]
    lines.extend(_marketplace_context_lines(marketplace=marketplace, details=marketplace_details))
    return "\n".join(lines)


def answer_question(*, client: AnthropicClient, order: dict, marketplace: str | None, question: str, marketplace_details: dict[str, Any] | None = None) -> str:
    context = _order_context_text(order=order, marketplace=marketplace, marketplace_details=marketplace_details)
    return client.complete(system=SYSTEM_PROMPT, user_message=f"Данные о заказе:\n{context}\n\nВопрос от сотрудника склада:\n{question}")
