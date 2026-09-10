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

    def stock_report(self) -> dict[str, Any]:
        return self._client.get("/report/stock/all")

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
