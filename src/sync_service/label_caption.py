from __future__ import annotations

from html import escape
from typing import Any


def format_items_html(items: list[dict[str, Any]]) -> str:
    """items: [{"sku": str, "count": int}, ...].

    HTML-formatted for Telegram (parse_mode=HTML): a line for count > 1 is
    bolded and marked with 🔴 — Telegram captions have no colored text, so
    this is the closest practical equivalent — to catch the cashier's eye
    when the same item is ordered twice. Shared between marketplaces
    (Yandex Market, OZON) so the label caption looks the same everywhere.
    """
    lines = []
    for item in items:
        sku = escape(str(item.get("sku") or "(пусто)"))
        count = item.get("count", 1)
        line = f"{sku} × {count}"
        lines.append(f"🔴 <b>{line}</b>" if count and count > 1 else line)
    return "\n".join(lines)


def build_caption(*, marketplace: str, store_name: str, order_label: str, items: list[dict[str, Any]]) -> str:
    """marketplace/store_name/order_label each get their own line, then one line per item."""
    return f"{marketplace}\n{store_name}\n{order_label}\n{format_items_html(items)}"
