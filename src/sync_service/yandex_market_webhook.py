from __future__ import annotations

import ipaddress
from typing import Any

from .yandex_market_order_sync import CAMPAIGN_NAMES
from .yandex_market_sync import YandexMarketSyncLog

# Yandex Market's published push-notification source ranges
# (https://yandex.ru/dev/market/partner-api/doc/ru/push-notifications/).
# There is no request signature — IP filtering is the only verification
# Yandex documents, so it only holds as long as nothing sits in front of
# this app that hides the real client IP (no reverse proxy today).
ALLOWED_NETWORKS = (
    ipaddress.ip_network("5.45.207.0/25"),
    ipaddress.ip_network("141.8.142.0/25"),
    ipaddress.ip_network("5.255.253.0/25"),
)


def is_allowed_ip(remote_addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(ip in network for network in ALLOWED_NETWORKS)


def _items_count(notification: dict[str, Any]) -> int:
    items = notification.get("items")
    return len(items) if isinstance(items, list) else 0


def _order_created_summary(n: dict[str, Any]) -> str:
    campaign_id = str(n.get("campaignId"))
    store_name = CAMPAIGN_NAMES.get(campaign_id, f"кампания {campaign_id}")
    return f"Новый заказ {n.get('orderId')} ({store_name}), позиций {_items_count(n)}"


_SUMMARIES: dict[str, Any] = {
    "PING": lambda n: "Проверочное уведомление (PING)",
    "ORDER_CREATED": _order_created_summary,
    "ORDER_STATUS_UPDATED": lambda n: f"Заказ {n.get('orderId')}: статус {n.get('status')}/{n.get('substatus')}",
    "ORDER_CANCELLED": lambda n: f"Заказ {n.get('orderId')} отменён",
    "ORDER_UPDATED": lambda n: f"Заказ {n.get('orderId')} изменён",
    "ORDER_CANCELLATION_REQUEST": lambda n: f"Запрос на отмену заказа {n.get('orderId')}",
    "ORDER_RETURN_CREATED": lambda n: f"Новый невыкуп/возврат по заказу {n.get('orderId')}",
    "ORDER_RETURN_STATUS_UPDATED": lambda n: f"Статус невыкупа/возврата изменён (заказ {n.get('orderId')})",
    "GOODS_FEEDBACK_CREATED": lambda n: "Новый отзыв о товаре",
    "GOODS_FEEDBACK_COMMENT_CREATED": lambda n: "Новый комментарий к отзыву",
    "CHAT_CREATED": lambda n: "Новый чат с покупателем",
    "CHAT_MESSAGE_SENT": lambda n: "Новое сообщение в чате",
    "CHAT_ARBITRAGE_STARTED": lambda n: "Начался спор по чату",
    "CHAT_ARBITRAGE_FINISHED": lambda n: "Спор по чату завершён",
    "QUESTION_CREATED": lambda n: "Новый вопрос о товаре",
    "QUESTION_ANSWER_CREATED": lambda n: "Новый ответ на вопрос",
    "QUESTION_COMMENT_CREATED": lambda n: "Новый комментарий к ответу на вопрос",
}


def summarize(notification: dict[str, Any]) -> str:
    notification_type = str(notification.get("notificationType") or "")
    build = _SUMMARIES.get(notification_type)
    return build(notification) if build else f"Неизвестный тип уведомления: {notification_type or '(пусто)'}"


def handle_notification(log: YandexMarketSyncLog, notification: dict[str, Any]) -> None:
    """Log a pushed notification. No external_id dedup key: Market can and does

    send several distinct events for the same order (status keeps moving), so
    every delivery is its own log row rather than being collapsed by the
    (kind, external_id) unique index used for the polling worker's snapshots.
    """
    notification_type = str(notification.get("notificationType") or "unknown").lower()
    log.add("webhook", notification_type, summarize(notification), None, notification)
