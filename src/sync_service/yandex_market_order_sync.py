from __future__ import annotations

from datetime import datetime
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
    return "\n".join(f"{item.get('offerId') or '(пусто)'} × {item.get('count', 1)}" for item in items)


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

    Idempotent on the MoySklad customerorder's externalCode: once that
    document exists, every later call for the same order_id is a no-op. This
    does not track partial failures per step (e.g. order created but label
    send failed) — a retry after a partial failure will skip everything,
    since document existence is the only idempotency signal. Known v1
    limitation; the error log still shows exactly which step failed so it can
    be finished by hand.
    """
    external_code = str(order_id)
    if moysklad.customer_order_by_external_code(external_code) is not None:
        return

    store_id = CAMPAIGN_STORES.get(str(campaign_id))
    if not store_id:
        log.add("order_pipeline_error", "error", f"Заказ {order_id}: неизвестная кампания {campaign_id}, склад не определён", None, {"order_id": order_id, "campaign_id": campaign_id})
        return

    order = yandex.order_by_id(order_id)
    if order is None:
        log.add("order_pipeline_error", "error", f"Заказ {order_id}: не удалось получить данные заказа из Яндекс.Маркета", None, {"order_id": order_id, "campaign_id": campaign_id})
        return

    items = order.get("items") or []
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

    label_pdf = yandex.get_order_label(order_id, campaign_id=str(campaign_id))
    if telegram is not None and telegram_chat_id:
        store_name = CAMPAIGN_NAMES.get(str(campaign_id), str(campaign_id))
        telegram.send_document(
            chat_id=telegram_chat_id,
            document=label_pdf,
            filename=f"{order_id}.pdf",
            caption=f"Яндекс.Маркет · {store_name} · заказ {order_id}\n{_format_items(items)}",
        )
        log.add("label_sent", "success", f"Заказ {order_id}: этикетка отправлена в Telegram", external_code)
    else:
        log.add("label_sent", "error", f"Заказ {order_id}: Telegram не настроен, этикетка не отправлена", external_code)
