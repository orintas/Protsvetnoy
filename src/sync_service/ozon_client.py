from __future__ import annotations

from typing import Any

from .http import ApiError, JsonClient

MAX_STOCKS_PER_REQUEST = 100  # OZON's own limit for POST /v2/products/stocks
PRODUCT_LIST_PAGE_SIZE = 1000


class OzonClient:
    """OZON Seller API client — lists the seller's offers, pushes per-warehouse
    stock, and handles the FBS posting→label flow (confirmed live against the
    real account: /v3/product/list, /v2/products/stocks, /v3/posting/fbs/get,
    /v2/posting/fbs/package-label)."""

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

    def posting_details(self, posting_number: str) -> dict[str, Any] | None:
        """Full posting info — products (with offer_id/sku/quantity), status,
        substatus, delivery_method (including warehouse_id). Returns None if
        OZON doesn't recognize the posting_number (e.g. a stale/bad webhook)."""
        try:
            payload = self._client.post(
                "/v3/posting/fbs/get",
                {"posting_number": posting_number, "with": {"analytics_data": True, "barcodes": True}},
            )
        except ApiError:
            return None
        result = payload.get("result")
        return result if isinstance(result, dict) else None

    def ship_posting(self, posting_number: str, products: list[dict[str, Any]]) -> None:
        """Confirms packaging (moves a posting from awaiting_packaging towards
        awaiting_deliver, after which its label becomes downloadable).

        products: the posting's own `products` list from posting_details —
        each needs `sku` (OZON's numeric sku, not offer_id) and `quantity`.
        Ported from the documented request shape (community client source,
        cross-checked against the changelog) — not live-tested end-to-end,
        since doing so would actually ship a real customer order.
        """
        packages = [{"products": [{"product_id": p["sku"], "quantity": p["quantity"]} for p in products]}]
        self._client.post("/v4/posting/fbs/ship", {"posting_number": posting_number, "packages": packages})

    def package_label(self, posting_numbers: list[str]) -> bytes:
        """Raw PDF bytes for the given postings' shipping labels — confirmed
        live: the response is a bare PDF body (content-type: application/pdf),
        not the JSON-wrapped {content_type, file_name, file_content} some
        older docs describe. Only works once a posting has reached
        awaiting_deliver; raises ApiError (message often literally "The next
        postings aren't ready") if called too early — callers should treat
        that as "not ready yet, retry later" rather than a hard failure."""
        return self._client.post_bytes("/v2/posting/fbs/package-label", {"posting_number": posting_numbers})
