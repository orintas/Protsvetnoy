from __future__ import annotations

import json
import re
from typing import Any, Protocol

from .moysklad import MoySkladClient
from .ozon_client import OzonClient
from .yandex_market import YandexMarketClient
from .yandex_market_sync import YandexMarketSyncLog


class LLMClient(Protocol):
    """Structural type for whichever LLM client is active (AnthropicClient,
    YandexGPTClient, ...) — this module only ever needs .complete()."""

    def complete(self, *, system: str, user_message: str, max_tokens: int = 1024, image_bytes: bytes | None = None, image_media_type: str = "image/jpeg") -> str: ...

MARKETPLACE_NAMES = {"yandex_market": "Яндекс.Маркет", "ozon": "OZON"}

DEFAULT_QUESTION = "Какой сейчас статус этого заказа?"

# The assistant can read anything in MoySklad/OZON/Yandex Market about the
# order (including courier contact details) but never claims to act —
# cancelling or any other order-state change stays a human action taken
# directly in MoySklad/the marketplace's own cabinet, per the explicit
# "propose but never execute on its own" constraint this feature was scoped
# under.
SYSTEM_PROMPT = """Ты — помощник склада цветочного магазина «Varvikas | Цветной» в рабочем чате Telegram, где сборщики читают этикетки заказов и иногда спрашивают про них — ответом на сообщение, номером заказа или фото чека/этикетки.

Тебе передают данные одного конкретного заказа (из МойСклад и, если есть, с площадки — Яндекс.Маркет или OZON, включая данные о курьере; для вопросов про остатки — текущий остаток в МойСклад на складе заказа и остаток, который сервис передавал на Яндекс.Маркет или OZON ближе к моменту заказа) и вопрос сотрудника о нём. Отвечай кратко, по-русски, только на основе переданных данных. Если в данных нет ответа на вопрос — так и скажи, не придумывай.

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
# numbers extracted wrong). (?<!\d)/(?!\d) lookarounds ensure a match always
# starts at the actual beginning of a digit run rather than silently
# sliding into the middle of a longer one when it doesn't fit the bound.
#
# \b (word boundary) was tried first and looked right, but broke on real
# messages like "заказу Nº62397236160": "º" (U+00BA MASCULINE ORDINAL
# INDICATOR — how "№" often gets typed from a Latin keyboard) is a Unicode
# letter, so \b sees no boundary between it and the digits that follow and
# the whole match silently fails — confirmed live 2026-09-30. Digit-adjacency
# lookarounds don't care what non-digit character comes before the number.
_ORDER_NUMBER_PATTERNS = (
    re.compile(r"(?<!\d)\d{4,15}-\d{2,8}-\d{1,4}(?!\d)"),  # OZON posting number, e.g. 87792534-0050-1
    re.compile(r"(?<!\d)\d{8,12}(?!\d)"),  # Yandex Market order id
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


def read_order_number_from_image(*, client: LLMClient, image_bytes: bytes, image_media_type: str = "image/jpeg") -> str | None:
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


def _store_id_from_order(order: dict) -> str | None:
    href = ((order.get("store") or {}).get("meta") or {}).get("href", "")
    return href.rstrip("/").split("/")[-1] or None


_MARKETPLACE_STOCK_LOG = {
    # (log attribute name chosen by the caller, sync-log kind, display name)
    "yandex_market": ("stock_sync", "Яндекс.Маркете"),
    "ozon": ("ozon_stock_sync", "OZON"),
}


def _closest_stock_history_lines(*, log: YandexMarketSyncLog, codes: list[str], kind: str, order_created_at: str | None) -> list[str]:
    """For each SKU, the stock-sync log entry closest to (at or before) the
    order's own creation time — i.e. what this service last told the
    marketplace the stock was around then. Prefers the precise before→after
    for that SKU from the entry's own payload (OZON logs one row per
    warehouse covering several SKUs at once, so the raw message text alone
    can omit a SKU if the preview was truncated); falls back to the full
    message when the payload doesn't help."""
    lines = []
    for code in codes:
        candidates = [e for e in log.search(code) if e.get("kind") == kind]
        if not candidates:
            continue
        candidates.sort(key=lambda e: e.get("created_at") or "")
        if order_created_at:
            before = [e for e in candidates if (e.get("created_at") or "") <= order_created_at]
            pick = before[-1] if before else candidates[0]
        else:
            pick = candidates[-1]
        change_text = None
        try:
            payload = json.loads(pick.get("payload") or "{}")
            for change in payload.get("changes") or []:
                if change.get("sku") == code:
                    before_val = change.get("before")
                    change_text = f"{code}: {before_val if before_val is not None else '—'}→{change.get('after')}"
                    break
        except (TypeError, ValueError):
            pass
        lines.append(f"{change_text or pick.get('message')} ({pick.get('created_at')})")
    return lines


def fetch_stock_context(*, moysklad: MoySkladClient, yandex_log: YandexMarketSyncLog, ozon_log: YandexMarketSyncLog | None = None, order: dict, marketplace: str | None, external_id: str | None) -> list[str]:
    """Best-effort: current MoySklad stock for every SKU in the order at its
    store, plus — for a Yandex Market or OZON order — the closest stock-sync
    entry this service logged for each SKU around when the order was created
    (what we told that marketplace the stock was at the time). Any failure
    here just leaves the stock section of the answer empty rather than
    failing the whole question — this mirrors fetch_marketplace_details's
    own contract.
    """
    lines: list[str] = []
    try:
        store_id = _store_id_from_order(order)
        order_id = order.get("id")
        if not store_id or not order_id:
            return lines
        positions = moysklad.customer_order_positions(order_id)
        codes: list[str] = []
        for position in positions:
            assortment = position.get("assortment") or {}
            code = assortment.get("code") or assortment.get("article")
            if code and code not in codes:
                codes.append(code)
        if not codes:
            return lines

        product_ids = []
        for position in positions:
            assortment = position.get("assortment") or {}
            if assortment.get("id"):
                product_ids.append(assortment["id"])
        stock_rows = moysklad.stock_by_products(product_ids, store_id=store_id) if product_ids else []
        by_code = {(row.get("code") or row.get("article")): row for row in stock_rows}
        stock_lines = []
        for code in codes:
            row = by_code.get(code)
            if row is None:
                stock_lines.append(f"{code}: 0 шт. на складе заказа (товар не числится в остатках)")
            else:
                stock_lines.append(f"{code}: остаток {row.get('stock', 0)}, резерв {row.get('reserve', 0)}, доступно {row.get('quantity', 0)}")
        if stock_lines:
            lines.append("Текущий остаток в МойСклад на складе заказа:")
            lines.extend(stock_lines)

        marketplace_log = {"yandex_market": yandex_log, "ozon": ozon_log}.get(marketplace or "")
        stock_kind_and_label = _MARKETPLACE_STOCK_LOG.get(marketplace or "")
        if marketplace_log is not None and stock_kind_and_label and external_id:
            kind, display_name = stock_kind_and_label
            order_created_at = None
            for entry in marketplace_log.search(external_id):
                if entry.get("kind") == "order_created" and entry.get("external_id") == external_id:
                    order_created_at = entry.get("created_at")
                    break
            marketplace_lines = _closest_stock_history_lines(log=marketplace_log, codes=codes, kind=kind, order_created_at=order_created_at)
            if marketplace_lines:
                lines.append(f"Остаток на {display_name} по нашей синхронизации (ближайшая запись к моменту заказа):")
                lines.extend(marketplace_lines)
    except Exception:
        return lines
    return lines


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


def _order_context_text(*, order: dict, marketplace: str | None, marketplace_details: dict[str, Any] | None = None, stock_lines: list[str] | None = None) -> str:
    state_name = (order.get("state") or {}).get("name", "неизвестен")
    header = f"Площадка: {MARKETPLACE_NAMES.get(marketplace, marketplace)}\n" if marketplace else ""
    lines = [
        f"{header}"
        f"Номер документа в МойСклад: {order.get('name')}\n"
        f"Статус в МойСклад: {state_name}\n"
        f"Состав заказа:\n{order.get('description') or '(нет данных)'}"
    ]
    lines.extend(_marketplace_context_lines(marketplace=marketplace, details=marketplace_details))
    if stock_lines:
        lines.append("\n".join(stock_lines))
    return "\n".join(lines)


def answer_question(*, client: LLMClient, order: dict, marketplace: str | None, question: str, marketplace_details: dict[str, Any] | None = None, stock_lines: list[str] | None = None) -> str:
    context = _order_context_text(order=order, marketplace=marketplace, marketplace_details=marketplace_details, stock_lines=stock_lines)
    return client.complete(system=SYSTEM_PROMPT, user_message=f"Данные о заказе:\n{context}\n\nВопрос от сотрудника склада:\n{question}")
