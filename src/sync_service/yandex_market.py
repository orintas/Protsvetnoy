from __future__ import annotations

from datetime import datetime
from typing import Any

from .http import JsonClient


class YandexMarketClient:
    """Client for the Yandex Market Partner API (FBS orders, stocks, prices, returns)."""

    def __init__(self, *, base_url: str, api_key: str, business_id: str) -> None:
        self._client = JsonClient(
            base_url=base_url,
            headers={
                "Api-Key": api_key,
                "Content-Type": "application/json",
            },
        )
        self._business_id = business_id

    def close(self) -> None:
        self._client.close()

    def campaigns(self) -> list[dict[str, Any]]:
        payload = self._client.get("/campaigns")
        rows = payload.get("campaigns", [])
        return [row for row in rows if isinstance(row, dict)]

    def orders(
        self,
        *,
        creation_date_from: datetime | None = None,
        creation_date_to: datetime | None = None,
        update_date_from: datetime | None = None,
        update_date_to: datetime | None = None,
        statuses: list[str] | None = None,
        order_ids: list[int] | None = None,
        page_token: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Fetch orders for the configured business (POST despite the "get" semantics)."""
        body: dict[str, Any] = {"limit": limit}
        dates: dict[str, str] = {}
        if creation_date_from:
            dates["creationDateFrom"] = creation_date_from.strftime("%d-%m-%Y")
        if creation_date_to:
            dates["creationDateTo"] = creation_date_to.strftime("%d-%m-%Y")
        if update_date_from:
            dates["updateDateFrom"] = update_date_from.strftime("%d-%m-%Y %H:%M:%S")
        if update_date_to:
            dates["updateDateTo"] = update_date_to.strftime("%d-%m-%Y %H:%M:%S")
        if dates:
            body["dates"] = dates
        if statuses:
            body["statuses"] = statuses
        if order_ids:
            body["orderIds"] = order_ids
        if page_token:
            body["pageToken"] = page_token
        return self._client.post(f"/v1/businesses/{self._business_id}/orders", body)

    def order_by_id(self, order_id: int) -> dict[str, Any] | None:
        payload = self.orders(order_ids=[order_id], limit=1)
        orders = payload.get("orders", [])
        return orders[0] if orders and isinstance(orders[0], dict) else None

    def all_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page_token = kwargs.pop("page_token", None)
        while True:
            payload = self.orders(page_token=page_token, **kwargs)
            orders = payload.get("orders", [])
            result.extend(order for order in orders if isinstance(order, dict))
            page_token = payload.get("paging", {}).get("nextPageToken")
            if not page_token:
                return result

    def update_order_status(self, order_id: int, *, campaign_id: str, status: str, substatus: str | None = None) -> dict[str, Any]:
        """PUT, not POST — POST on this path returns HTTP 405 (confirmed live)."""
        order: dict[str, Any] = {"status": status}
        if substatus:
            order["substatus"] = substatus
        return self._client.put(
            f"/v2/campaigns/{campaign_id}/orders/{order_id}/status",
            {"order": order},
        )

    def campaign_offers(self, campaign_id: str) -> list[str]:
        """Every offerId listed in this campaign, regardless of status (including NO_STOCKS).

        Paginated via query-string `limit`/`page_token` (not the JSON body,
        unlike most other endpoints in this client) — confirmed against the
        live API, since the docs don't make this explicit.
        """
        offer_ids: list[str] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 200}
            if page_token:
                params["page_token"] = page_token
            payload = self._client.post_with_query(f"/v2/campaigns/{campaign_id}/offers", params=params, body={})
            result = payload.get("result", {})
            offer_ids.extend(str(o["offerId"]) for o in result.get("offers", []) if isinstance(o, dict) and o.get("offerId"))
            page_token = result.get("paging", {}).get("nextPageToken")
            if not page_token:
                return offer_ids

    def update_stocks(self, items: list[dict[str, Any]], *, campaign_id: str, warehouse_id: int) -> dict[str, Any]:
        """items: [{"sku": ..., "count": int}, ...] (max 2000).

        Must be PUT, not POST: POST on this exact path silently no-ops and
        returns an unrelated paginated stock listing instead of applying
        anything — confirmed against the live API (identical response body
        for any POST payload, including an obviously-wrong sentinel count).
        `type: "FIT"` is the physical/settable count; Yandex derives
        `AVAILABLE` itself from it minus its own pending reservations.
        """
        skus = [
            {"sku": item["sku"], "warehouseId": warehouse_id, "items": [{"type": "FIT", "count": item["count"]}]}
            for item in items
        ]
        return self._client.put(f"/v2/campaigns/{campaign_id}/offers/stocks", {"skus": skus})

    def update_prices(self, items: list[dict[str, Any]], *, campaign_id: str) -> dict[str, Any]:
        """items: [{"offer_id": ..., "value": float, "currency_id": "RUR", "vat": int_optional}, ...] (max 2000)."""
        offers = []
        for item in items:
            price: dict[str, Any] = {"value": item["value"], "currencyId": item.get("currency_id", "RUR")}
            if item.get("vat") is not None:
                price["vat"] = item["vat"]
            offers.append({"offerId": item["offer_id"], "price": price})
        return self._client.post(f"/v2/campaigns/{campaign_id}/offer-prices/updates", {"offers": offers})

    def get_order_label(self, order_id: int, *, campaign_id: str) -> bytes:
        """PDF with the shipping label(s) for every box in the order."""
        return self._client.get_bytes(f"/v2/campaigns/{campaign_id}/orders/{order_id}/delivery/labels")

    def returns(
        self,
        *,
        campaign_id: str,
        from_date: str | None = None,
        to_date: str | None = None,
        return_type: str | None = None,
        page_token: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        if return_type:
            params["type"] = return_type
        if page_token:
            params["page_token"] = page_token
        return self._client.get(f"/v2/campaigns/{campaign_id}/returns", params=params)
