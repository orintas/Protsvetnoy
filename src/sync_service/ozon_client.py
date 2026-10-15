from __future__ import annotations

from typing import Any

from .change_log import record
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
        for item in items:
            record(service="ozon", entity_type="offer.stock", entity_id=f"{warehouse_id}:{item['sku']}", action="update", after=item["count"])

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
        record(service="ozon", entity_type="posting.ship", entity_id=posting_number, action="update", after={"products": len(products)})

    def package_label(self, posting_numbers: list[str]) -> bytes:
        """Raw PDF bytes for the given postings' shipping labels — confirmed
        live: the response is a bare PDF body (content-type: application/pdf),
        not the JSON-wrapped {content_type, file_name, file_content} some
        older docs describe. Only works once a posting has reached
        awaiting_deliver; raises ApiError (message often literally "The next
        postings aren't ready") if called too early — callers should treat
        that as "not ready yet, retry later" rather than a hard failure."""
        return self._client.post_bytes("/v2/posting/fbs/package-label", {"posting_number": posting_numbers})

    def notification_list(self) -> dict[str, Any]:
        """Registered push-notification subscriptions — confirmed live shape:
        {"urls": [{"url", "enable", "availability_status" (GREEN/YELLOW/RED), ...}], "total_count", ...}."""
        return self._client.post("/v1/notification/list", {})

    def unprocessed_reviews(self, limit: int = 100) -> list[dict[str, Any]]:
        """Reviews with status UNPROCESSED, newest first, paginated via
        last_id/has_next (confirmed live shape). `limit` is per OZON's own
        page-size bound of [20, 100] — values outside that range are clamped
        rather than left to fail the request."""
        page_size = max(20, min(limit, 100))
        reviews: list[dict[str, Any]] = []
        last_id = ""
        while True:
            body: dict[str, Any] = {"status": "UNPROCESSED", "limit": page_size, "sort_dir": "DESC"}
            if last_id:
                body["last_id"] = last_id
            payload = self._client.post("/v1/review/list", body)
            reviews.extend(payload.get("reviews") or [])
            if not payload.get("has_next"):
                return reviews
            last_id = payload.get("last_id") or ""
            if not last_id:
                return reviews

    def product_names_by_sku(self, skus: list[int]) -> dict[int, dict[str, str]]:
        """{sku: {"offer_id", "name"}} for a batch of OZON SKUs, via
        /v3/product/info/list. The response doesn't echo the queried sku on
        the item itself — it's nested under `sources[].sku` instead (every
        source sku maps to the same offer_id/name) — confirmed live
        2026-10-14."""
        if not skus:
            return {}
        payload = self._client.post("/v3/product/info/list", {"sku": skus})
        result: dict[int, dict[str, str]] = {}
        for item in payload.get("items") or []:
            offer_id = item.get("offer_id") or ""
            name = item.get("name") or ""
            for source in item.get("sources") or []:
                sku = source.get("sku")
                if sku is not None:
                    result[sku] = {"offer_id": offer_id, "name": name}
        return result

    def create_review_comment(self, *, review_id: str, text: str, mark_review_as_processed: bool = True) -> str:
        """Posts a top-level seller reply on a review (no parent_comment_id —
        that's only for replying to an existing comment thread, which this
        service never does). Returns the new comment_id. Field names per
        OZON's own help-center walkthrough and the community Go client's
        matching struct — not yet confirmed against a live response, unlike
        every other method here; verify on the first real send."""
        payload = self._client.post("/v1/review/comment/create", {
            "review_id": review_id,
            "text": text,
            "mark_review_as_processed": mark_review_as_processed,
        })
        return str(payload.get("comment_id") or "")
