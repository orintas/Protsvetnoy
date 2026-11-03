from __future__ import annotations

import hmac
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .moysklad_links import moysklad_link_fields
from .telegram_client import TelegramClient
from .tilda_stock_sync import SITE_STORES
from .yandex_market_sync import YandexMarketSyncLog

MOSCOW = ZoneInfo("Europe/Moscow")

ORGANIZATION_ID = "40b2d5fc-22a4-11ec-0a80-02b1002197e3"  # ООО "ЦВЕТНОЙ МИР"
# Same owner/department as the Yandex Market orders from this organization,
# so the staff who pack them can see them (see yandex_market_order_sync).
OWNER_EMPLOYEE_ID = "04d2ad06-669c-11ee-0a80-0951004f1ad4"  # Вероника Рябцева
GROUP_ID = "08e6b024-d269-11e4-90a2-8ecb0004d9d2"  # "Основной"
SALES_CHANNEL_NAME = "Сайт protsvetnoy.com"
# There's no "Оплачен" state in this account; "Подтвержден" is the closest
# (checked live 2026-11-02 against /entity/customerorder/metadata).
PAID_STATE_ID = "876e83e2-2e73-11eb-0a80-01df00075b5a"  # "Подтвержден"
# The service already used for delivery on ~3400 existing online orders.
DELIVERY_SERVICE_ID = "6c327f06-15a6-11e4-9288-002590a28eca"  # "Доставка в интернете"
# Counterparty "groups" in the MoySklad UI are tags (lowercase).
COUNTERPARTY_TAG = "сайт"

# Pickup option text in the Tilda cart (agreed with the Tilda side) -> store
# key. Matched by these words anywhere in the option, case-insensitively;
# everything else (Яндекс Доставка: курьер, ПВЗ, постамат) ships from "main".
PICKUP_WORDS: dict[str, str] = {
    "авиапарк": "aviapark",
    "мега химки": "megakhimki",
    "ривьера": "riviera",
    "саларис": "salaris",
}
STORE_TITLES: dict[str, str] = {
    "main": "Основной склад",
    "aviapark": "ТЦ «Авиапарк»",
    "megakhimki": "ТРЦ «МЕГА Химки»",
    "riviera": "ТЦ «Ривьера»",
    "salaris": "ТЦ «Саларис»",
}

MAX_ATTEMPTS = 10
RETRY_AFTER = timedelta(minutes=2)
# A "processing" claim older than this belongs to a process that died
# mid-order (deploy restart) and may be taken over.
STALE_CLAIM = timedelta(minutes=10)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip().lower()


def store_key_for_delivery(delivery: str) -> str:
    normalized = _normalize(delivery)
    for word, key in PICKUP_WORDS.items():
        if word in normalized:
            return key
    return "main"


def _number(value: Any) -> float:
    try:
        return float(str(value).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _nest(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """`payment[products][0][name]=…` form fields -> nested dicts/lists."""
    root: dict[str, Any] = {}
    for key, value in pairs:
        parts = [part for part in re.split(r"[\[\]]+", key) if part != ""] or [key]
        node = root
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value

    def listify(node: Any) -> Any:
        if isinstance(node, dict):
            node = {key: listify(value) for key, value in node.items()}
            if node and all(key.isdigit() for key in node):
                return [node[key] for key in sorted(node, key=int)]
        return node

    return listify(root)


def parse_webhook_body(raw: bytes, content_type: str) -> dict[str, Any]:
    """Tilda posts either JSON or a form; in a form, `payment` arrives as
    bracketed fields or as one JSON-encoded field ("Передавать данные о
    товарах массивом"). All three end up as the same nested dict."""
    text = raw.decode("utf-8", errors="replace")
    if "json" in (content_type or "").lower() or text.lstrip().startswith("{"):
        data = json.loads(text) if text.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("Tilda webhook body is not a JSON object")
    else:
        data = _nest(parse_qsl(text, keep_blank_values=True))
    payment = data.get("payment")
    if isinstance(payment, str) and payment.strip().startswith("{"):
        data["payment"] = json.loads(payment)
    return data


def is_test_request(data: dict[str, Any]) -> bool:
    return str(data.get("test") or "").lower() == "test"


def has_valid_key(data: dict[str, Any], headers: dict[str, str], secret: str) -> bool:
    """Tilda can send the receiver's API key either as a header or as a
    form field, under a name set in its settings — so any header or
    top-level field holding exactly the secret counts."""
    if not secret:
        return False
    candidates = list(headers.values()) + [value for value in data.values() if isinstance(value, str)]
    return any(hmac.compare_digest(candidate.strip().encode("utf-8"), secret.encode("utf-8")) for candidate in candidates)


def redact_key(data: dict[str, Any], secret: str) -> dict[str, Any]:
    return {key: value for key, value in data.items() if not (secret and isinstance(value, str) and value.strip() == secret)}


def _field(data: dict[str, Any], *names: str) -> str:
    lowered = {str(key).lower(): value for key, value in data.items()}
    for name in names:
        value = lowered.get(name.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def order_id_of(data: dict[str, Any]) -> str:
    payment = data.get("payment") if isinstance(data.get("payment"), dict) else {}
    return str(payment.get("orderid") or "").strip()


def normalize_phone(phone: str) -> list[str]:
    """The spellings MoySklad holds for the same Russian number: +7…, 7…, 8…"""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        return [phone.strip()] if phone and phone.strip() else []
    return [f"+7{digits}", f"7{digits}", f"8{digits}"]


class TildaOrderQueue:
    """Every accepted webhook is written here before Tilda gets its 200, so
    a paid order survives a crash or a deploy restart mid-processing. A row
    is claimed atomically before work starts — the web process (right after
    the webhook) and the worker (retries) can both pick up the same row."""

    def __init__(self, path: str = "data/tilda_orders.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS site_orders (
                order_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, error TEXT)"""
            )

    def enqueue(self, order_id: str, payload: dict[str, Any]) -> bool:
        """False when the order is already queued (Tilda resent it)."""
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO site_orders(order_id, received_at, payload, status, updated_at) VALUES (?,?,?,?,?)",
                (order_id, now, json.dumps(payload, ensure_ascii=False), "pending", now),
            )
            return cursor.rowcount == 1

    def claim(self, order_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or datetime.now(timezone.utc)
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                """UPDATE site_orders SET status='processing', attempts=attempts+1, updated_at=?
                WHERE order_id=? AND attempts < ? AND (status IN ('pending','failed') OR (status='processing' AND updated_at < ?))""",
                (now.isoformat(), order_id, MAX_ATTEMPTS, (now - STALE_CLAIM).isoformat()),
            )
            if cursor.rowcount != 1:
                return None
            row = db.execute("SELECT received_at, payload, attempts FROM site_orders WHERE order_id=?", (order_id,)).fetchone()
        return {"order_id": order_id, "received_at": row[0], "payload": json.loads(row[1]), "attempts": row[2]}

    def finish(self, order_id: str, *, error: str | None = None) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE site_orders SET status=?, error=?, updated_at=? WHERE order_id=?",
                ("failed" if error else "done", error, datetime.now(timezone.utc).isoformat(), order_id),
            )

    def retryable(self, *, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                """SELECT order_id FROM site_orders WHERE attempts < ? AND (
                status='pending' AND updated_at < ? OR status='failed' AND updated_at < ? OR status='processing' AND updated_at < ?)
                ORDER BY received_at""",
                (MAX_ATTEMPTS, (now - timedelta(seconds=30)).isoformat(), (now - RETRY_AFTER).isoformat(), (now - STALE_CLAIM).isoformat()),
            ).fetchall()
        return [row[0] for row in rows]


def _moment(received_at: str) -> str:
    try:
        moment = datetime.fromisoformat(received_at)
    except ValueError:
        moment = datetime.now(timezone.utc)
    return moment.astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M:%S.000")


_sales_channel_id: str | None = None


def _sales_channel(moysklad: MoySkladClient) -> str:
    global _sales_channel_id
    if _sales_channel_id is None:
        channel = moysklad.sales_channel_by_name(SALES_CHANNEL_NAME) or moysklad.create_sales_channel(name=SALES_CHANNEL_NAME, channel_type="ECOMMERCE")
        _sales_channel_id = channel["id"]
    return _sales_channel_id


def _counterparty(moysklad: MoySkladClient, *, name: str, phone: str, email: str) -> dict[str, Any]:
    phones = normalize_phone(phone)
    found = moysklad.counterparty_by_phone(phones) or moysklad.counterparty_by_email(email)
    if found is not None:
        return found
    return moysklad.create_counterparty(
        name=name or phone or email or "Покупатель сайта", email=email, phone=phones[0] if phones else "",
        group_id=GROUP_ID, tags=[COUNTERPARTY_TAG], company_type="individual",
    )


def _products(payment: dict[str, Any]) -> list[dict[str, Any]]:
    products = payment.get("products") or []
    if isinstance(products, dict):
        products = list(products.values())
    return [p if isinstance(p, dict) else {"name": str(p)} for p in products]


def _discount_percent(payment: dict[str, Any], products: list[dict[str, Any]]) -> float:
    """Tilda's per-product price is before a promo code; the paid total
    isn't. The gap is spread over every position as MoySklad's own
    percentage discount, so the order sums to what was actually paid."""
    list_total = sum(_number(p.get("price")) * (_number(p.get("quantity")) or 1) for p in products)
    goods_paid = _number(payment.get("amount")) - _number(payment.get("delivery_price"))
    if list_total <= 0 or goods_paid <= 0 or goods_paid >= list_total - 0.005:
        return 0.0
    return round((1 - goods_paid / list_total) * 100, 4)


EXTRA_DELIVERY_SKIP = {"delivery", "delivery_price", "delivery_address", "delivery_comment"}


def _description(order_id: str, data: dict[str, Any], payment: dict[str, Any], buyer: dict[str, str], missing: list[str]) -> str:
    lines = [f"Заказ с сайта protsvetnoy.com №{order_id}"]
    if payment.get("delivery"):
        lines.append(f"Способ получения: {payment['delivery']}")
    if payment.get("delivery_address"):
        lines.append(f"Адрес: {payment['delivery_address']}")
    # Whatever else Tilda's Yandex Delivery module adds (ПВЗ, получатель…).
    for key, value in payment.items():
        if str(key).startswith("delivery") and key not in EXTRA_DELIVERY_SKIP and isinstance(value, str) and value.strip():
            lines.append(f"{key}: {value.strip()}")
    comment = payment.get("delivery_comment") or _field(data, "comment", "комментарий")
    if comment:
        lines.append(f"Комментарий: {comment}")
    if payment.get("promocode"):
        lines.append(f"Промокод: {payment['promocode']}")
    lines.append("Покупатель: " + ", ".join(value for value in (buyer["name"], buyer["phone"], buyer["email"]) if value))
    if missing:
        lines.append("Не найдены в МойСклад: " + ", ".join(missing))
    return "\n".join(lines)


def _buyer(data: dict[str, Any]) -> dict[str, str]:
    return {
        "name": _field(data, "name", "fio", "фио", "имя"),
        "phone": _field(data, "phone", "телефон"),
        "email": _field(data, "email", "e-mail", "почта"),
    }


def notification_text(*, order_id: str, store_key: str, moysklad_name: str, products: list[dict[str, Any]], buyer: dict[str, str], payment: dict[str, Any]) -> str:
    pickup = store_key != "main"
    first = f"{STORE_TITLES[store_key]} — самовывоз" if pickup else "Доставка Яндексом — Основной склад"
    lines = [first, f"Заказ с сайта №{order_id}" + (f" (МойСклад {moysklad_name})" if moysklad_name else "")]
    for p in products:
        quantity = _number(p.get("quantity")) or 1
        lines.append(f"• {p.get('sku') or '—'} × {quantity:g} — {p.get('name') or ''}".rstrip(" —"))
    lines.append("Покупатель: " + ", ".join(value for value in (buyer["name"], buyer["phone"]) if value))
    if not pickup:
        if payment.get("delivery"):
            lines.append(f"Способ: {payment['delivery']}")
        if payment.get("delivery_address"):
            lines.append(f"Адрес: {payment['delivery_address']}")
    return "\n".join(lines)


def process_order(
    *, order_id: str, data: dict[str, Any], received_at: str, moysklad: MoySkladClient, telegram: TelegramClient | None,
    settings: Settings, log: YandexMarketSyncLog, errors: ErrorLog,
) -> None:
    """A paid Tilda order -> MoySklad customerorder with every position in
    reserve, then a Telegram notice. Both steps are idempotent (externalCode
    and the log's "order_notified" row), so a retry resumes where it failed.

    An unknown sku never blocks the order — it's already paid: the order is
    created with whatever matched and the rest is listed in its description."""
    external_code = f"tilda-{order_id}"
    payment = data.get("payment") if isinstance(data.get("payment"), dict) else {}
    products = _products(payment)
    buyer = _buyer(data)
    store_key = store_key_for_delivery(str(payment.get("delivery") or ""))

    existing = moysklad.customer_order_by_external_code(external_code)
    if existing is None:
        discount = _discount_percent(payment, products)
        positions: list[dict[str, Any]] = []
        missing: list[str] = []
        for p in products:
            sku = str(p.get("sku") or "").strip()
            quantity = _number(p.get("quantity")) or 1
            product = moysklad.product_by_code(sku) if sku else None
            if product is None:
                missing.append(f"{sku or p.get('name') or '(без артикула)'} ×{quantity:g}")
                continue
            position: dict[str, Any] = {"quantity": quantity, "reserve": quantity, "price": round(_number(p.get("price")) * 100), "assortment": {"meta": product["meta"]}}
            if discount:
                position["discount"] = discount
            positions.append(position)
        delivery_price = _number(payment.get("delivery_price"))
        if delivery_price > 0:
            positions.append({
                "quantity": 1, "price": round(delivery_price * 100),
                "assortment": {"meta": {"href": f"{moysklad.base_url}/entity/service/{DELIVERY_SERVICE_ID}", "type": "service", "mediaType": "application/json"}},
            })
        agent = _counterparty(moysklad, name=buyer["name"], phone=buyer["phone"], email=buyer["email"])
        existing = moysklad.create_customer_order(
            moment=_moment(received_at), organization_id=ORGANIZATION_ID, agent_id=agent["id"], store_id=SITE_STORES[store_key],
            external_code=external_code, positions=positions, description=_description(order_id, data, payment, buyer, missing),
            sales_channel_id=_sales_channel(moysklad), state_id=PAID_STATE_ID, owner_id=OWNER_EMPLOYEE_ID, group_id=GROUP_ID,
        )
        log.add("order_created", "success", f"Заказ {order_id}: создан в МойСклад, {STORE_TITLES[store_key]}, позиций {len(positions)}",
                order_id, {"store": store_key, **moysklad_link_fields(existing, f"Заказ {existing.get('name') or order_id}")})
        if missing:
            message = f"Заказ с сайта {order_id}: не найдены в МойСклад по коду: {', '.join(missing)} — заказ создан без них"
            log.add("order_missing_products", "error", message, order_id, {"missing": missing})
            errors.add("tilda_order_sync", message)

    if log.has_success("order_notified", order_id):
        return
    # The marketplaces' label chat by default; the per-kind chats only override it.
    chat_id = (settings.telegram_site_delivery_chat_id if store_key == "main" else settings.telegram_site_pickup_chat_id) or settings.telegram_label_chat_id
    if telegram is None or not chat_id:
        log.add("order_notified", "skipped", f"Заказ {order_id}: чат Telegram для «{STORE_TITLES[store_key]}» не задан, уведомление не отправлено", order_id)
        return
    telegram.send_message(chat_id=chat_id, text=notification_text(
        order_id=order_id, store_key=store_key, moysklad_name=str(existing.get("name") or ""), products=products, buyer=buyer, payment=payment,
    ))
    log.add("order_notified", "success", f"Заказ {order_id}: уведомление отправлено в Telegram", order_id)


def process_queued(order_id: str, settings: Settings, queue: TildaOrderQueue, log: YandexMarketSyncLog, errors: ErrorLog) -> None:
    item = queue.claim(order_id)
    if item is None:
        return
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    telegram = TelegramClient(bot_token=settings.telegram_bot_token, proxy=settings.telegram_proxy_url) if settings.telegram_bot_token else None
    try:
        process_order(order_id=order_id, data=item["payload"], received_at=item["received_at"], moysklad=moysklad, telegram=telegram,
                      settings=settings, log=log, errors=errors)
    except Exception as error:
        queue.finish(order_id, error=str(error))
        final = item["attempts"] >= MAX_ATTEMPTS
        log.add("order_error", "error", f"Заказ {order_id}: ошибка обработки (попытка {item['attempts']}" + (", последняя" if final else "") + f"): {error}",
                f"{order_id}:{item['attempts']}")
        errors.log_exception("tilda_order_sync", error, context=f"Заказ с сайта {order_id}" + (": попытки исчерпаны, нужен ручной ввод" if final else ""))
        return
    finally:
        moysklad.close()
        if telegram is not None:
            telegram.close()
    queue.finish(order_id)


def retry_pending(settings: Settings, queue: TildaOrderQueue, log: YandexMarketSyncLog, errors: ErrorLog) -> None:
    for order_id in queue.retryable():
        process_queued(order_id, settings, queue, log, errors)
