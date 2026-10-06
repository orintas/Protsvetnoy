from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .label_caption import build_caption, format_items_plain
from .moysklad import MoySkladClient
from .moysklad_links import moysklad_link_fields
from .order_lock import yandex_market_order_pipeline
from .telegram_client import TelegramClient
from .yandex_market import YandexMarketClient
from .yandex_market_sync import YandexMarketSyncLog

# MoySklad entities fixed for every new Yandex Market order — confirmed against
# a real order created 2026-09-13 and explicit choices from the person running
# this service (see conversation history, not derivable from the API alone).
ORGANIZATION_ID = "40b2d5fc-22a4-11ec-0a80-02b1002197e3"  # ООО "ЦВЕТНОЙ МИР"
AGENT_ID = "fa685205-1cbb-11e8-9107-5048000779ee"  # ООО "ЯНДЕКС.МАРКЕТ", ИНН 7704357909
SALES_CHANNEL_ID = "5a2f722a-549c-11ef-0a80-0493000bcbe7"  # "Яндекс Маркет FBS"
# Orders created under the sync's own API token were invisible to the staff
# member who actually packs them (same issue confirmed for the Shopify
# pipeline 2026-09-30 — see shopify_order_sync.OWNER_EMPLOYEE_ID). Owning
# new orders to her directly, in the main department, fixes that at the
# source. Per explicit correction: Yandex Market orders go to Вероника
# Рябцева / "Основной", not the Shopify pipeline's Спицын / "ProTsvetnoy OU".
OWNER_EMPLOYEE_ID = "04d2ad06-669c-11ee-0a80-0951004f1ad4"  # Вероника Рябцева
GROUP_ID = "08e6b024-d269-11e4-90a2-8ecb0004d9d2"  # "Основной"

# MoySklad customerorder workflow states (GET /entity/customerorder/metadata), matched
# to Yandex Market's own order status/substatus pushed via ORDER_STATUS_UPDATED.
DELIVERING_STATE_ID = "ea763d96-9bb8-11ed-0a80-0076000cbaed"  # "Доставляется"
COMPLETED_STATE_ID = "8e499530-ac67-11e4-7a40-e89700075e01"  # "Выполнен"
CANCELLED_STATE_ID = "ad2312d4-a7f7-11e2-fb80-001b21d91495"  # "Отменен" — stateType "Unsuccessful", releases the reserve automatically

LABEL_RETRY_KIND = "label_retry_error"
MAX_LABEL_RETRIES = 3

# Yandex Market's CANCELLED substatus codes -> a human-readable reason, for
# the Telegram notice. Falls back to the raw code for anything not listed
# here rather than hiding it.
CANCEL_REASON_LABELS: dict[str, str] = {
    "USER_CHANGED_MIND": "покупатель передумал",
    "USER_UNREACHABLE": "не удалось связаться с покупателем",
    "USER_REFUSED_DELIVERY": "покупатель отказался от способа доставки",
    "USER_REFUSED_QUALITY": "покупатель отказался — претензии к качеству",
    "USER_REFUSED_PRODUCT": "покупатель отказался от товара",
    "USER_REFUSED_ADDRESS_CHANGE": "не согласован новый адрес доставки",
    "USER_NOT_PAID": "заказ не был оплачен",
    "USER_BOUGHT_CHEAPER": "покупатель нашёл дешевле",
    "RESERVATION_EXPIRED": "истёк срок резервирования",
    "PROCESSING_EXPIRED": "истёк срок обработки заказа",
    "SHOP_FAILED": "магазин не смог выполнить заказ",
    "REPLACING_ORDER": "заказ заменён другим",
}


def _cancel_reason_text(substatus: str | None) -> str:
    if not substatus:
        return "причина не указана"
    return CANCEL_REASON_LABELS.get(substatus, substatus)


# Substatus codes that mean the shop itself failed to fulfill (as opposed to
# the buyer cancelling, a timeout, or an administrative replacement) — these
# are the ones worth an automatic stock write-off, since the book stock told
# us we had it and we didn't.
SHOP_FAULT_SUBSTATUSES = {"SHOP_FAILED"}

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
        caption_items = [{"sku": item.get("offerId"), "count": item.get("count", 1)} for item in items]
        result = telegram.send_document(
            chat_id=telegram_chat_id,
            document=label_pdf,
            filename=f"{order_id}.pdf",
            caption=build_caption(marketplace="Яндекс.Маркет", store_name=store_name, order_label=f"Заказ №{order_id}", items=caption_items),
            parse_mode="HTML",
        )
        # Kept so a later cancellation can reply to this exact message instead
        # of posting an unrelated standalone notice — see handle_order_cancelled.
        message_id = (result.get("result") or {}).get("message_id")
        log.add("label_sent", "success", f"Заказ {order_id}: этикетка отправлена в Telegram", external_code, {"message_id": message_id})
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
    # Market can (and does) resend ORDER_CREATED on webhook failure; the
    # checks above aren't atomic against MoySklad/the log, so a second
    # near-simultaneous delivery for the same order must be serialized
    # against the first rather than run concurrently — otherwise both can
    # pass a check before either acts on it (a duplicate MoySklad order, or
    # a duplicate label sent to Telegram). Confirmed live 2026-09-29 for the
    # analogous Shopify pipeline; see order_lock.py.
    with yandex_market_order_pipeline:
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

            description_items = [{"sku": item.get("offerId"), "count": item.get("count", 1)} for item in items]
            description = format_items_plain(description_items)
            created_order = moysklad.create_customer_order(
                name=str(order_id),
                moment=_moysklad_moment(order.get("creationDate")),
                organization_id=ORGANIZATION_ID,
                agent_id=AGENT_ID,
                store_id=store_id,
                external_code=external_code,
                positions=positions,
                description=description,
                sales_channel_id=SALES_CHANNEL_ID,
                owner_id=OWNER_EMPLOYEE_ID,
                group_id=GROUP_ID,
            )
            log.add("order_created", "success", f"Заказ {order_id}: создан в МойСклад ({len(positions)} позиций)", external_code, {**order, **moysklad_link_fields(created_order, f"Заказ {order_id}")})

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
    for the same order into a single row. Locked against
    yandex_market_order_pipeline: a duplicate delivery of the same
    status-update webhook could otherwise pass the has_success check twice
    before either send commits, sending the label to Telegram twice.
    """
    with yandex_market_order_pipeline:
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

    previous_state_id = _previous_state_id(order)
    moysklad.update_customer_order_state(str(order["id"]), state_id, previous_state_id=previous_state_id)
    log.add("order_state_updated", "success", f"Заказ {order_id}: статус в МойСклад изменён на «{label}»", external_code, moysklad_link_fields(order, f"Заказ {order_id}"))


def _previous_state_id(order: dict[str, Any]) -> str | None:
    href = ((order.get("state") or {}).get("meta") or {}).get("href", "")
    return href.rsplit("/", 1)[-1] or None


COURIER_NOTIFIED_KIND = "courier_notified"

# Confirmed against a real completed order's full status history
# (PROCESSING/STARTED -> READY_TO_SHIP -> COURIER_SEARCH -> COURIER_FOUND ->
# COURIER_ARRIVED_TO_SENDER -> DELIVERY/COURIER_RECEIVED ->
# DELIVERED/DELIVERY_SERVICE_DELIVERED): the courier showing up at our store
# is its own substatus, still under PROCESSING — a full status/substatus
# step before the order ever reaches DELIVERY.
COURIER_ARRIVED_SUBSTATUS = "COURIER_ARRIVED_TO_SENDER"


def notify_courier_arrived(
    *,
    order_id: int,
    campaign_id: int,
    status: str | None,
    substatus: str | None,
    telegram: TelegramClient | None,
    telegram_chat_id: str,
    log: YandexMarketSyncLog,
) -> None:
    """Notice for the courier physically arriving at our store to collect
    the order. Fires once per order; replies to the original label message
    when we still have its message_id on record. Locked against
    yandex_market_order_pipeline: a duplicate delivery of the same
    status-update webhook could otherwise pass the has_success check twice
    before either send commits, notifying Telegram twice."""
    if substatus != COURIER_ARRIVED_SUBSTATUS:
        return
    if telegram is None or not telegram_chat_id:
        return
    with yandex_market_order_pipeline:
        external_code = str(order_id)
        if log.has_success(COURIER_NOTIFIED_KIND, external_code):
            return
        store_name = CAMPAIGN_NAMES.get(str(campaign_id), str(campaign_id))
        label_payload = log.get_payload("label_sent", external_code)
        reply_to = (label_payload or {}).get("message_id") if isinstance(label_payload, dict) else None
        telegram.send_message(
            chat_id=telegram_chat_id,
            text=f"🚚 Приехал курьер в {store_name} за заказом {order_id}",
            reply_to_message_id=reply_to,
        )
        log.add(COURIER_NOTIFIED_KIND, "success", f"Заказ {order_id}: уведомление о курьере отправлено в Telegram", external_code)


def _create_loss_for_cancelled_order(*, moysklad: MoySkladClient, order: dict[str, Any], campaign_id: int, order_id: int, log: YandexMarketSyncLog, external_code: str) -> str | None:
    """SHOP_FAILED means Market (and the book stock we fed it) said the item
    was available and it wasn't — write off the order's positions at its own
    store right away so the same SKU doesn't oversell again on the next
    order, instead of leaving that correction to be found by hand later (as
    happened in the real incident this responds to: a product's book stock
    stayed wrong for days before it tripped up another order).

    Best-effort and independently idempotency-guarded via "loss_created" —
    this must never run twice for the same order even if a later step in the
    same call (e.g. the Telegram send) fails and the whole function gets
    retried on a redelivered webhook. Any failure here is logged but never
    blocks the cancellation notice itself.
    """
    if log.has_success("loss_created", external_code):
        return None
    store_id = CAMPAIGN_STORES.get(str(campaign_id))
    if not store_id:
        return None
    try:
        positions = moysklad.customer_order_positions(str(order["id"]))
        loss_positions = []
        for position in positions:
            assortment = position.get("assortment") or {}
            quantity = position.get("quantity")
            if not assortment.get("meta") or not quantity:
                continue
            loss_positions.append({"quantity": quantity, "assortment": {"meta": assortment["meta"]}})
        if not loss_positions:
            return None
        result = moysklad.create_loss(
            organization_id=ORGANIZATION_ID,
            store_id=store_id,
            positions=loss_positions,
            description=f"Автосписание: заказ {order_id} отменён площадкой как SHOP_FAILED — товар числился в наличии по данным МойСклад, но не найден при сборке.",
        )
        log.add("loss_created", "success", f"Заказ {order_id}: создано списание в МойСклад ({len(loss_positions)} поз.)", external_code, moysklad_link_fields(result, f"Списание по заказу {order_id}"))
        return f"📦 Создано списание в МойСклад: {result.get('name')} — проверьте физический остаток."
    except Exception as error:
        log.add("loss_create_error", "error", f"Заказ {order_id}: не удалось создать списание в МойСклад: {error}", external_code)
        return None


def handle_order_cancelled(
    *,
    order_id: int,
    campaign_id: int,
    substatus: str | None,
    moysklad: MoySkladClient,
    telegram: TelegramClient | None,
    telegram_chat_id: str,
    log: YandexMarketSyncLog,
) -> None:
    """A Market cancellation: move the MoySklad order to "Отменен" (its
    "Unsuccessful" stateType releases the reserve automatically — no
    separate reservation call), then notify Telegram, replying to the
    original label message when we still have its message_id on record.

    Guarded by has_success on "cancel_notified" and locked against
    yandex_market_order_pipeline: Market can resend the same
    ORDER_STATUS_UPDATED webhook (a documented, expected retry), and without
    either of those this function had no idempotency at all on the Telegram
    step — every redelivery sent a second cancellation notice.
    """
    external_code = str(order_id)
    with yandex_market_order_pipeline:
        if log.has_success("cancel_notified", external_code):
            return
        reason = _cancel_reason_text(substatus)
        order = moysklad.customer_order_by_external_code(external_code)
        loss_line = None
        if order is None:
            log.add("order_cancel_error", "error", f"Заказ {order_id}: не найден в МойСклад, статус «Отменен» не проставлен", external_code)
        else:
            previous_state_id = _previous_state_id(order)
            moysklad.update_customer_order_state(str(order["id"]), CANCELLED_STATE_ID, previous_state_id=previous_state_id)
            log.add("order_cancelled", "success", f"Заказ {order_id}: статус в МойСклад изменён на «Отменен» ({reason}), резерв снят", external_code, moysklad_link_fields(order, f"Заказ {order_id}"))
            if substatus in SHOP_FAULT_SUBSTATUSES:
                loss_line = _create_loss_for_cancelled_order(moysklad=moysklad, order=order, campaign_id=campaign_id, order_id=order_id, log=log, external_code=external_code)

        if telegram is not None and telegram_chat_id:
            store_name = CAMPAIGN_NAMES.get(str(campaign_id), str(campaign_id))
            text = f"❌ Заказ №{order_id} ({store_name}) отменён.\nПричина: {reason}"
            if loss_line:
                text += f"\n{loss_line}"
            label_payload = log.get_payload("label_sent", external_code)
            reply_to = (label_payload or {}).get("message_id") if isinstance(label_payload, dict) else None
            telegram.send_message(chat_id=telegram_chat_id, text=text, reply_to_message_id=reply_to)
            log.add("cancel_notified", "success", f"Заказ {order_id}: уведомление об отмене отправлено в Telegram", external_code)
