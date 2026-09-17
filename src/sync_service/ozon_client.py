from __future__ import annotations

from typing import Any

from .http import JsonClient

MAX_STOCKS_PER_REQUEST = 100  # OZON's own limit for POST /v2/products/stocks
PRODUCT_LIST_PAGE_SIZE = 1000


class OzonClient:
    """OZON Seller API client — just enough to list the seller's offers and
    push per-warehouse stock counts (confirmed live against the real
    account: /v3/product/list, /v2/products/stocks)."""

    def __init__(self, *, client_id: str, api_key: str, base_url: str = "https://api-seller.ozon.ru") -> None:
        self._client = JsonClient(base_url=base_url, headers={"Client-Id": client_id, "Api-Key": api_key})

    def close(self) -> None:
        self._client.close()

    def product_offer_ids(self) -> list[str]:
        """Every non-archived offer_id in the seller's catalog, paginated via last_id."""
        offer_ids: list[str] = []
        last_id = ""
        while True:
            payload = self._client.post("/v3/product/list", {"filter": {}, "last_id": last_id, "limit": PRODUCT_LIST_PAGE_SIZE})
            result = payload.get("result", {})
            items = result.get("items", [])
            offer_ids.extend(str(item["offer_id"]) for item in items if isinstance(item, dict) and item.get("offer_id") and not item.get("archived"))
            last_id = result.get("last_id") or ""
            if not last_id or not items:
                return offer_ids

    def update_stocks(self, items: list[dict[str, Any]], *, warehouse_id: int) -> None:
        """items: [{"sku": offer_id, "count": int}, ...]. Chunked at 100 per
        request internally — OZON rejects a larger single request."""
        stocks = [{"offer_id": item["sku"], "stock": item["count"], "warehouse_id": warehouse_id} for item in items]
        for i in range(0, len(stocks), MAX_STOCKS_PER_REQUEST):
            self._client.post("/v2/products/stocks", {"stocks": stocks[i : i + MAX_STOCKS_PER_REQUEST]})
