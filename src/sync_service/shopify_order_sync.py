from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .moysklad import MoySkladClient
from .shopify_sync import ShopifySyncLog


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    """Shopify signs every webhook body with HMAC-SHA256 over the raw bytes,
    base64-encoded, in the X-Shopify-Hmac-Sha256 header — this is the only
    authenticity check Shopify webhooks have (no source-IP allowlist)."""
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)

# Fixed MoySklad entities for every Shopify customer order — confirmed live
# against real, manually-entered Shopify orders in this account (salesChannel,
# organization, agent placeholders) and cross-checked against the API directly.
ORGANIZATION_ID = "666e33cc-1b25-11ea-0a80-061e0003c973"  # Varvikas Grupp OU
SALES_CHANNEL_ID = "233f92ab-d2d8-11ed-0a80-10df000eaa30"  # "Shopify"
CURRENCY_ID = "5100bbd5-1cec-11ea-0a80-04b1000ad00d"  # EUR
PLN_CURRENCY_ID = "cae74fea-26ec-11ee-0a80-02b4000b49e4"  # PLN, злотый — Polish orders are billed in PLN, not EUR
SHIPPING_STATE_ID = "dd675d83-a396-11e2-c56e-001b21d91495"  # customerorder state "Отгружать"

# Baltic + Finland: ship from the main warehouse first, Ulemiste as fallback.
MAIN_STORE_ID = "d9a80084-1bf4-11ea-0a80-057b000493d9"  # warehouse "ProTsvetnoy OU"
ULEMISTE_STORE_ID = "70633779-2d92-11ec-0a80-00c500080605"  # "Ulemiste keskus"
BALTIC_COUNTRIES = {"FI", "EE", "LV", "LT"}
BALTIC_WAREHOUSE_CHAIN = [MAIN_STORE_ID, ULEMISTE_STORE_ID]

# Rest of Europe: Wola Park first, then any other active Poland store —
# the full live list under the Poland organization (entity/retailstore),
# not just the subset config/store-mappings.json covers for Novicloud.
WOLA_PARK_STORE_ID = "78c6a61b-5e17-11ef-0a80-0605003cce71"
POLAND_WAREHOUSE_CHAIN = [
    WOLA_PARK_STORE_ID,
    "a337abc2-968f-11ef-0a80-08f60006abcb",  # Wroclavia
    "f63e3123-2500-11ef-0a80-005500195c81",  # Galeria Lodzka
    "23775a80-2a61-11ee-0a80-114d00018cb3",  # Janki
    "49adc2de-b44c-11ee-0a80-1034000b6054",  # Port Lodz
    "228f10d5-f28c-11ef-0a80-190d0028d86c",  # Magnolia Park
    "257287ff-7888-11ee-0a80-063200072e4e",  # Westfield Mokotow
    "13f41b67-015e-11ef-0a80-1713000fa8d3",  # Manufaktura
    "6d74cf78-a66d-11f0-0a80-0fd70015ef7b",  # Arkadia
]

# Shipping country -> the generic per-country buyer placeholder MoySklad
# already uses for manually-entered Shopify orders — kept as-is rather than
# matching/creating real customers, per explicit request.
COUNTRY_AGENTS: dict[str, str] = {
    "EE": "3cf97e15-b4b0-11ee-0a80-095d0001d988",
    "LT": "7c511bf5-b4b0-11ee-0a80-01020001e44b",
    "LV": "66721ff4-b4b0-11ee-0a80-15c60001ff4f",
    "FI": "975c21af-b4b0-11ee-0a80-14f00001b4aa",
    "PL": "5af328d9-27f7-11ef-0a80-0eba0029c762",
    "FR": "478166ae-9001-11f1-0a80-05a8001d996d",
    "DE": "6705a507-5e26-11f0-0a80-05bc0012fc76",
    "NL": "6db33a4c-1d5a-11f1-0a80-07e700ce79fc",
    "RO": "74fbe42f-1d30-11f1-0a80-052800c3e92d",
    "AT": "77514edd-8f1f-11f1-0a80-108700762ace",
    "HR": "800cfa50-1881-11f1-0a80-1a840018f932",
    "SE": "b016c73f-5e4e-11f1-0a80-025900147e3c",
    "HU": "bb763be9-4d1e-11f1-0a80-1b5900928968",
    "MT": "15892c7e-6bd8-11f1-0a80-05fb001c1df0",
    "SI": "1ae4bba7-5fe2-11f1-0a80-08f60011e582",
}

MOSCOW = ZoneInfo("Europe/Moscow")


def _moysklad_moment(iso_moment: str | None) -> str:
    if iso_moment:
        try:
            return datetime.fromisoformat(iso_moment).astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M:%S.000")
        except ValueError:
            pass
    return datetime.now(MOSCOW).strftime("%Y-%m-%d %H:%M:%S.000")


def _warehouse_chain(country_code: str) -> list[str]:
    return BALTIC_WAREHOUSE_CHAIN if country_code in BALTIC_COUNTRIES else POLAND_WAREHOUSE_CHAIN


def _pick_store(moysklad: MoySkladClient, country_code: str, skus_needed: dict[str, int]) -> str:
    """First warehouse in the country's chain with enough of every ordered
    SKU; the chain's last warehouse if none fully qualifies — the order still
    needs to go somewhere, and the last store is the account's own fallback
    of last resort in both chains."""
    chain = _warehouse_chain(country_code)
    for store_id in chain:
        stock = {row.get("code"): row.get("quantity") or 0 for row in moysklad.stock_by_store(store_id)}
        if all(stock.get(sku, 0) >= qty for sku, qty in skus_needed.items()):
            return store_id
    return chain[-1]


def _format_address(order: dict[str, Any]) -> str:
    address = order.get("shipping_address") or {}
    parts = [
        address.get("name"),
        address.get("address1"),
        address.get("address2"),
        address.get("city"),
        address.get("zip"),
        address.get("country"),
        address.get("phone"),
    ]
    return ", ".join(str(part) for part in parts if part)


def _line_item_price(item: dict[str, Any], *, use_presentment: bool) -> float:
    """Shopify's `price` field (and price_set.shop_money, which always
    matches it) is in the shop's base currency (EUR). Polish orders need
    the actual PLN amount the customer was charged — that's
    price_set.presentment_money, same per-unit granularity as `price`."""
    if use_presentment:
        presentment = ((item.get("price_set") or {}).get("presentment_money") or {}).get("amount")
        if presentment is not None:
            return float(presentment)
    return float(item.get("price") or 0)


def _build_positions(moysklad: MoySkladClient, line_items: list[dict[str, Any]], *, use_presentment_price: bool) -> tuple[list[dict[str, Any]], list[str]]:
    positions: list[dict[str, Any]] = []
    missing_skus: list[str] = []
    for item in line_items:
        sku = str(item.get("sku") or "")
        product = moysklad.product_by_code(sku) if sku else None
        if product is None:
            missing_skus.append(sku or "(пусто)")
            continue
        positions.append({
            "quantity": item.get("quantity") or 1,
            "price": round(_line_item_price(item, use_presentment=use_presentment_price) * 100),
            "assortment": {"meta": product["meta"]},
        })
    return positions, missing_skus


def process_new_order(*, order: dict[str, Any], moysklad: MoySkladClient, log: ShopifySyncLog) -> None:
    """Create a MoySklad customerorder for a new Shopify order (orders/create webhook).

    Idempotent on the order's numeric Shopify id (externalCode). Warehouse is
    picked per shipping country (see _pick_store); the buyer is always one of
    the account's existing per-country e-shop placeholder counterparties, not
    a real customer record.
    """
    external_code = str(order.get("id") or "")
    order_name = str(order.get("name") or external_code)
    if not external_code:
        log.add("order_error", "error", "Заказ Shopify без id — пропущен", None, order)
        return
    if moysklad.customer_order_by_external_code(external_code) is not None:
        return

    country_code = str((order.get("shipping_address") or {}).get("country_code") or "").upper()
    agent_id = COUNTRY_AGENTS.get(country_code)
    if not agent_id:
        log.add("order_error", "error", f"Заказ {order_name}: страна доставки «{country_code or '?'}» не настроена, покупатель не определён", external_code, order)
        return

    is_poland = country_code == "PL"
    currency_id = PLN_CURRENCY_ID if is_poland else CURRENCY_ID

    line_items = order.get("line_items") or []
    positions, missing_skus = _build_positions(moysklad, line_items, use_presentment_price=is_poland)
    if missing_skus:
        log.add("order_error", "error", f"Заказ {order_name}: товары не найдены в МойСклад по артикулу: {', '.join(missing_skus)}", external_code, order)
    if not positions:
        log.add("order_error", "error", f"Заказ {order_name}: ни одной позиции не удалось сопоставить, заказ не создан", external_code, order)
        return

    skus_needed: dict[str, int] = {}
    for item in line_items:
        sku = str(item.get("sku") or "")
        if sku:
            skus_needed[sku] = skus_needed.get(sku, 0) + (item.get("quantity") or 1)
    store_id = _pick_store(moysklad, country_code, skus_needed)

    address = _format_address(order)
    description = f"{order_name}\nАдрес доставки: {address}" if address else order_name
    moysklad.create_customer_order(
        moment=_moysklad_moment(order.get("created_at")),
        organization_id=ORGANIZATION_ID,
        agent_id=agent_id,
        store_id=store_id,
        external_code=external_code,
        positions=positions,
        description=description,
        sales_channel_id=SALES_CHANNEL_ID,
        currency_id=currency_id,
        state_id=SHIPPING_STATE_ID,
    )
    log.add("order_created", "success", f"Заказ {order_name}: создан в МойСклад ({len(positions)} позиций)", external_code, order)
