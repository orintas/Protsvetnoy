from __future__ import annotations

from .anthropic_client import AnthropicClient
from .moysklad import MoySkladClient
from .yandex_market_sync import YandexMarketSyncLog

MARKETPLACE_NAMES = {"yandex_market": "Яндекс.Маркет", "ozon": "OZON"}

# The assistant only ever answers from the order data it's given and never
# claims to act — cancelling (or any other order-state change) stays a human
# action taken directly in MoySklad/the marketplace's own cabinet, per the
# explicit "propose but never execute on its own" constraint this feature was
# scoped under.
SYSTEM_PROMPT = """Ты — помощник склада цветочного магазина «Varvikas | Цветной» в рабочем чате Telegram, где сборщики читают этикетки заказов и иногда отвечают на них с вопросами.

Тебе передают данные одного конкретного заказа и вопрос сотрудника о нём. Отвечай кратко, по-русски, только на основе переданных данных о заказе. Если в данных нет ответа на вопрос — так и скажи, не придумывай.

Если сотрудник просит отменить заказ или явно выражает намерение его отменить — ты не можешь отменить заказ сам, у тебя нет такой возможности. Объясни это и скажи, что отмену нужно сделать вручную: в МойСклад и (если нужно, чтобы покупатель тоже узнал) в личном кабинете соответствующей площадки. Никогда не пиши, что заказ отменён или что ты его отменяешь.

Пиши обычным текстом, без markdown-разметки и заголовков — это сообщение в чат."""


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


def _order_context_text(*, marketplace: str, external_id: str, moysklad: MoySkladClient) -> str | None:
    order = moysklad.customer_order_by_external_code(external_id) if marketplace == "yandex_market" else moysklad.customer_order_by_name(external_id)
    if order is None:
        return None
    state_name = (order.get("state") or {}).get("name", "неизвестен")
    return (
        f"Площадка: {MARKETPLACE_NAMES.get(marketplace, marketplace)}\n"
        f"Номер заказа/отправления: {external_id}\n"
        f"Номер документа в МойСклад: {order.get('name')}\n"
        f"Статус в МойСклад: {state_name}\n"
        f"Состав заказа:\n{order.get('description') or '(нет данных)'}"
    )


def answer_question(*, client: AnthropicClient, marketplace: str, external_id: str, moysklad: MoySkladClient, question: str) -> str | None:
    """None means "no such order in MoySklad" — caller decides whether/how
    to tell the staff member that, rather than this module guessing at a
    user-facing message for a case it can't actually help with."""
    context = _order_context_text(marketplace=marketplace, external_id=external_id, moysklad=moysklad)
    if context is None:
        return None
    return client.complete(system=SYSTEM_PROMPT, user_message=f"Данные о заказе:\n{context}\n\nВопрос от сотрудника склада:\n{question}")
