from __future__ import annotations

from typing import Any

from .http import JsonClient


class MoySkladClient:
    def __init__(self, *, base_url: str, token: str) -> None:
        self._client = JsonClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json;charset=utf-8",
            },
        )

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def stock_report(self) -> dict[str, Any]:
        return self._client.get("/report/stock/all")

    def documents(self, entity: str, *, document_filter: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Fetch all rows of a document entity (e.g. retaildemand) matching a filter."""
        payload = self._client.get(f"/entity/{entity}", params={"filter": document_filter, "limit": limit})
        result: list[dict[str, Any]] = []
        while True:
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                raise ValueError(f"MoySklad {entity} response has invalid rows")
            result.extend(row for row in rows if isinstance(row, dict))
            next_link = payload.get("meta", {}).get("nextHref") if isinstance(payload.get("meta"), dict) else None
            if not next_link:
                return result
            payload = self._client.get_url(str(next_link))

    def products(self) -> list[dict[str, Any]]:
        payload = self._client.get("/entity/product", params={"limit": 1000})
        result: list[dict[str, Any]] = []
        while True:
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                raise ValueError("MoySklad products response has invalid rows")
            result.extend(row for row in rows if isinstance(row, dict))
            next_link = payload.get("meta", {}).get("nextHref") if isinstance(payload.get("meta"), dict) else None
            if not next_link:
                return result
            payload = self._client.get_url(str(next_link))

    def create_retail_sale(
        self,
        *,
        name: str,
        moment: str,
        store_id: str,
        retail_store_id: str,
        retail_shift_id: str,
        organization_id: str,
        external_code: str,
        positions: list[dict[str, Any]],
        cash_sum: float,
        non_cash_sum: float,
    ) -> dict[str, Any]:
        return self._client.post(
            "/entity/retaildemand",
            {
                "name": name,
                "moment": moment,
                "store": {
                    "meta": {
                        "href": f"{self._client.base_url}/entity/store/{store_id}",
                        "type": "store",
                        "mediaType": "application/json",
                    }
                },
                "retailStore": {
                    "meta": {
                        "href": f"{self._client.base_url}/entity/retailstore/{retail_store_id}",
                        "type": "retailstore",
                        "mediaType": "application/json",
                    }
                },
                "retailShift": {
                    "meta": {
                        "href": f"{self._client.base_url}/entity/retailshift/{retail_shift_id}",
                        "type": "retailshift",
                        "mediaType": "application/json",
                    }
                },
                "organization": {
                    "meta": {
                        "href": f"{self._client.base_url}/entity/organization/{organization_id}",
                        "type": "organization",
                        "mediaType": "application/json",
                    }
                },
                "externalCode": external_code,
                "vatEnabled": True,
                "vatIncluded": True,
                "cashSum": cash_sum,
                "noCashSum": non_cash_sum,
                "positions": positions,
            },
        )

    def close(self) -> None:
        self._client.close()
