from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from .moysklad import MoySkladClient
from .telegram_client import TelegramClient
from .yandex_market import YandexMarketClient
from .yandex_market_sync import YandexMarketSyncLog

# MoySklad entities fixed for every new Yandex Market order — confirmed against
# a real order created 2026-09-13 and explicit choices from the person running
# this service (see conversation history, not derivable from the API alone).
ORGANIZATION_ID = "40b2d5fc-22a4-11ec-0a80-02b1002197e3"  # ООО "ЦВЕТНОЙ МИР"
AGENT_ID = "fa685205-1cbb-11e8-9107-5048000779ee"  # ООО "ЯНДЕКС.МАРКЕТ", ИНН 7704357909
SALES_CHANNEL_ID = "5a2f722a-549c-11ef-0a80-0493000bcbe7"  # "Яндекс Маркет FBS"

# MoySklad customerorder workflow states (GET /entity/customerorder/metadata), matched
# to Yandex Market's own order status/substatus pushed via ORDER_STATUS_UPDATED.
DELIVERING_STATE_ID = "ea763d96-9bb8-11ed-0a80-0076000cbaed"  # "Доставляется"
COMPLETED_STATE_ID = "8e499530-ac67-11e4-7a40-e89700075e01"  # "Выполнен"

LABEL_RETRY_KIND = "label_retry_error"
MAX_LABEL_RETRIES = 3

# campaignId -> MoySklad store id, one per physical shop.
CAMPAIGN_STORES: dict[str, str] = {
    "149179204": "497d98c2-7e21-11ee-0a80-0e2a000dc91f",  # ТЦ Авиапарк
    "149179258": "0caf123c-6e09-11f0-0a80-00c900243b0d",  # ТЦ Саларис
    "149179260": "e1222420-16f2-11ed-0a80-010b002a9d1c",  # ТЦ Ривьера
    "149179270": "dc9b7c0e-a66d-11eb-0a80-09b9002a05ad",  # ТЦ Мега Химки
}

# campaignId -> the campaign's own display name in the Yandex Market seller
# cabinet (GET /campaigns) — used everywhere in logs/UI instead of the raw
# numeric id, which means nothing to a human at a glance.
CAMPAIGN_NAMES: dict[str, str] = {
    "149179204": "ТМ Авиапарк",
    "149179258": "ТЦ Саларис",
    "149179260": "ТЦ Ривьера",
    "149179270": "ТЦ МЕГА Химки",
}

# campaignId -> the FBS warehouse id Yandex Market assigned this shop
# (Настройки → Остатки → Склады) — confirmed both from the live API
# (POST /v2/campaigns/{id}/offers/stocks response's warehouses[].warehouseId)
# and from a screenshot of that page. Required by the stock-update PUT call;
# distinct from campaignId and not derivable from it.
CAMPAIGN_WAREHOUSES: dict[str, int] = {
    "149179204": 2346691,  # ТМ Авиапарк
    "149179258": 2346756,  # ТЦ Саларис
    "149179260": 2346759,  # ТЦ Ривьера
    "149179270": 2346789,  # ТЦ МЕГА Химки
}

MOSCOW = ZoneInfo("Europe/Moscow")


def _moysklad_moment(iso_moment: str | None) -> str:
    if iso_moment:
        try:
            return datetime.fromisoformat(iso_moment).astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M:%S.000")
        except ValueError:
            pass
    return datetime.now(MOSCOW).strftime("%Y-%m-%d %H:%M:%S.000")


def _format_items(items: list[dict[str, Any]]) -> str:
    """HTML-formatted for Telegram (parse_mode=HTML): a line for count > 1 is
    bolded and marked with 🔴 — Telegram captions have no colored text, so
    this is the closest practical equivalent — to catch the cashier's eye
    when the same item is ordered twice."""
    lines = []
    for item in items:
        sku = escape(str(item.get("offerId") or "(пусто)"))
        count = item.get("count", 1)
        line = f"{sku} × {count}"
        lines.append(f"🔴 <b>{line}</b>" if count and count > 1 else line)
    return "\n".join(lines)


def _build_positions(moysklad: MoySkladClient, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    positions: list[dict[str, Any]] = []
    missing_codes: list[str] = []
    for item in items:
        code = str(item.get("offerId") or "")
        product = moysklad.product_by_code(code) if code else None
        if product is None:
            missing_codes.append(code or "(пусто)")
            continue
        price_value = ((item.get("prices") or {}).get("payment") or {}).get("value", 0)
        positions.append({
            "quantity": item.get("count", 1),
            "price": round(float(price_value) * 100),
            "assortment": {"meta": product["meta"]},
        })
    return positions, missing_codes


def _send_label(
    *,
    order_id: int,
    campaign_id: int,
    items: list[dict[str, Any]],
    yandex: YandexMarketClient,
    telegram: TelegramClient | None,
    telegram_chat_id: str,
    log: YandexMarketSyncLog,
    external_code: str,
) -> bool:
    """Returns whether the label was actually sent (False only for the
    "Telegram isn't configured" case — a real send failure raises instead)."""
    label_pdf = yandex.get_order_label(order_id, campaign_id=str(campaign_id))
    if telegram is not None and telegram_chat_id:
        store_name = CAMPAIGN_NAMES.get(str(campaign_id), str(campaign_id))
        telegram.send_document(
            chat_id=telegram_chat_id,
            document=label_pdf,
            filename=f"{order_id}.pdf",
            caption=f"Яндекс.Маркет\n{store_name}\nЗаказ №{order_id}\n{_format_items(items)}",
            parse_mode="HTML",
        )
        log.add("label_sent", "success", f"Заказ {order_id}: этикетка отправлена в Telegram", external_code)
        return True
    log.add("label_sent", "error", f"Заказ {order_id}: Telegram не настроен, этикетка не отправлена", external_code)
    return False


def process_new_order(
    *,
    order_id: int,
    campaign_id: int,
    moysklad: MoySkladClient,
    yandex: YandexMarketClient,
    telegram: TelegramClient | None,
    telegram_chat_id: str,
    log: YandexMarketSyncLog,
) -> None:
    """Create the MoySklad order, confirm assembly, and push the label.

    Idempotent on the MoySklad customerorder's externalCode, but resumable
    past that: if the order already exists (created on an earlier attempt)
    and the label was never confirmed sent, a retry (Market resends
    ORDER_CREATED on webhook failure) picks up at the label step instead of
    doing nothing — order creation and assembly confirmation are not
    re-attempted, since MoySklad/Market's own idempotency for those isn't
    guaranteed the way the label step's is (via has_success).
    """
    external_code = str(order_id)
    order_created_already = moysklad.customer_order_by_external_code(external_code) is not None
    if order_created_already and log.has_success("label_sent", external_code):
        return

    store_id = None
    if not order_created_already:
        store_id = CAMPAIGN_STORES.get(str(campaign_id))
        if not store_id:
            log.add("order_pipeline_error", "error", f"Заказ {order_id}: неизвестная кампания {campaign_id}, склад не определён", None, {"order_id": order_id, "campaign_id": campaign_id})
            return

    order = yandex.order_by_id(order_id)
    if order is None:
        log.add("order_pipeline_error", "error", f"Заказ {order_id}: не удалось получить данные заказа из Яндекс.Маркета", None, {"order_id": order_id, "campaign_id": campaign_id})
        return
    items = order.get("items") or []

    if not order_created_already:
        positions, missing_codes = _build_positions(moysklad, items)
        if missing_codes:
            log.add("order_pipeline_error", "error", f"Заказ {order_id}: товары не найдены в МойСклад по коду: {', '.join(missing_codes)}", None, order)
        if not positions:
            log.add("order_pipeline_error", "error", f"Заказ {order_id}: ни одной позиции не удалось сопоставить, заказ не создан", None, order)
            return

        description = "Заказанные артикулы: " + ", ".join(str(item.get("offerId") or "") for item in items)
        moysklad.create_customer_order(
            name=str(order_id),
            moment=_moysklad_moment(order.get("creationDate")),
            organization_id=ORGANIZATION_ID,
            agent_id=AGENT_ID,
            store_id=store_id,
            external_code=external_code,
            positions=positions,
            description=description,
            sales_channel_id=SALES_CHANNEL_ID,
        )
        log.add("order_created", "success", f"Заказ {order_id}: создан в МойСклад ({len(positions)} позиций)", external_code, order)

        yandex.update_order_status(order_id, campaign_id=str(campaign_id), status="PROCESSING", substatus="READY_TO_SHIP")
        log.add("assembly_confirmed", "success", f"Заказ {order_id}: сборка подтверждена на Яндекс.Маркете", external_code)

    _send_label(order_id=order_id, campaign_id=campaign_id, items=items, yandex=yandex, telegram=telegram, telegram_chat_id=telegram_chat_id, log=log, external_code=external_code)


def retry_label_if_missing(
    *,
    order_id: int,
    campaign_id: int,
    moysklad: MoySkladClient,
    yandex: YandexMarketClient,
    telegram: TelegramClient | None,
    telegram_chat_id: str,
    log: YandexMarketSyncLog,
) -> None:
    """Best-effort nudge, called alongside every delivery-status update: if
    the order exists in MoySklad but its label was never confirmed sent (the
    original attempt hit e.g. a transient Telegram-proxy failure), try again —
    up to MAX_LABEL_RETRIES times total. Attempts are tracked as numbered log
    rows (":1", ":2", ...) rather than a plain count, since the log's own
    (kind, external_id) uniqueness would otherwise collapse repeat failures
    for the same order into a single row.
    """
    external_code = str(order_id)
    if log.has_success("label_sent", external_code):
        return
    if moysklad.customer_order_by_external_code(external_code) is None:
        return  # order was never created — not this function's job to fix

    attempts_so_far = log.count_matching(LABEL_RETRY_KIND, f"{external_code}:")
    if attempts_so_far >= MAX_LABEL_RETRIES:
        return
    attempt = attempts_so_far + 1

    try:
        order = yandex.order_by_id(order_id)
        if order is None:
            raise RuntimeError("не удалось получить заказ из Яндекс.Маркета")
        items = order.get("items") or []
        sent = _send_label(order_id=order_id, campaign_id=campaign_id, items=items, yandex=yandex, telegram=telegram, telegram_chat_id=telegram_chat_id, log=log, external_code=external_code)
        if not sent:
            raise RuntimeError("Telegram не настроен")
    except Exception as error:
        log.add(LABEL_RETRY_KIND, "error", f"Заказ {order_id}: повторная попытка {attempt}/{MAX_LABEL_RETRIES} отправить этикетку не удалась: {error}", f"{external_code}:{attempt}")


def sync_order_delivery_state(
    *,
    order_id: int,
    status: str | None,
    substatus: str | None,
    moysklad: MoySkladClient,
    log: YandexMarketSyncLog,
) -> None:
    """Mirror a Market delivery status push onto the MoySklad customerorder's state.

    DELIVERY (handed to the delivery service, substatus DELIVERY_SERVICE_RECEIVED
    included) -> "Доставляется"; DELIVERED -> "Выполнен". Every other
    status/substatus is ignored — this only tracks the delivery tail, not the
    whole order lifecycle.
    """
    if status == "DELIVERED":
        state_id, label = COMPLETED_STATE_ID, "Выполнен"
    elif status == "DELIVERY" or substatus == "DELIVERY_SERVICE_RECEIVED":
        state_id, label = DELIVERING_STATE_ID, "Доставляется"
    else:
        return

    external_code = str(order_id)
    order = moysklad.customer_order_by_external_code(external_code)
    if order is None:
        log.add("order_state_error", "error", f"Заказ {order_id}: не найден в МойСклад, статус «{label}» не проставлен", external_code)
        return

    moysklad.update_customer_order_state(str(order["id"]), state_id)
    log.add("order_state_updated", "success", f"Заказ {order_id}: статус в МойСклад изменён на «{label}»", external_code)
