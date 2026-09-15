from __future__ import annotations

from datetime import datetime
from typing import Any

from .http import JsonClient

# The "ProTsvetnoy OU" group — the org's non-Russian retail arm. Product
# categories live under this group; Russia-side categories are excluded.
PROTSVETNOY_GROUP_ID = "62a11082-1b25-11ea-0a80-030300038a2c"


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

    def products_by_category(self, path_name: str) -> list[dict[str, Any]]:
        """Non-archived products in one category (pathName), with images expanded."""
        payload = self._client.get("/entity/product", params={"filter": f"pathName={path_name}", "limit": 1000, "expand": "images"})
        result: list[dict[str, Any]] = []
        while True:
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                raise ValueError("MoySklad products response has invalid rows")
            result.extend(row for row in rows if isinstance(row, dict) and not row.get("archived"))
            next_link = payload.get("meta", {}).get("nextHref") if isinstance(payload.get("meta"), dict) else None
            if not next_link:
                return result
            payload = self._client.get_url(str(next_link))

    def product_image_bytes(self, product: dict[str, Any]) -> bytes | None:
        rows = (product.get("images") or {}).get("rows") or []
        if not rows:
            return None
        download_href = rows[0].get("meta", {}).get("downloadHref")
        return self._client.get_bytes_url(download_href) if download_href else None

    def product_categories(self, group_id: str = PROTSVETNOY_GROUP_ID) -> list[dict[str, Any]]:
        """Non-archived top-level product folders belonging to a MoySklad group."""
        href = f"{self._client.base_url}/entity/group/{group_id}"
        payload = self._client.get("/entity/productfolder", params={"filter": f"group={href}", "limit": 100})
        result: list[dict[str, Any]] = []
        while True:
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                raise ValueError("MoySklad productfolder response has invalid rows")
            result.extend(row for row in rows if isinstance(row, dict) and not row.get("archived"))
            next_link = payload.get("meta", {}).get("nextHref") if isinstance(payload.get("meta"), dict) else None
            if not next_link:
                return result
            payload = self._client.get_url(str(next_link))

    def _meta(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        return {"meta": {"href": f"{self._client.base_url}/entity/{entity_type}/{entity_id}", "type": entity_type, "mediaType": "application/json"}}

    def product_by_code(self, code: str) -> dict[str, Any] | None:
        payload = self._client.get("/entity/product", params={"filter": f"code={code}", "limit": 1})
        rows = payload.get("rows", [])
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def warehouses_by_organization(self, org_id: str) -> list[dict[str, Any]]:
        """Physical warehouses (entity/store) backing each active retail store for one organization.

        There's no direct organization filter on entity/store itself, so this
        goes through retailstore (which does have one) and follows its
        store link — confirmed live: every retail store here maps 1:1 to its
        own warehouse.
        """
        href = f"{self._client.base_url}/entity/organization/{org_id}"
        payload = self._client.get("/entity/retailstore", params={"filter": f"organization={href}", "limit": 100, "expand": "store"})
        result: list[dict[str, Any]] = []
        for row in payload.get("rows", []):
            if row.get("archived"):
                continue
            store = row.get("store") or {}
            if store.get("id"):
                result.append({"id": store["id"], "name": store.get("name") or row.get("name")})
        return result

    def stock_by_store(self, store_id: str) -> list[dict[str, Any]]:
        """Sellable stock (quantity = stock - reserve) per product for one store."""
        href = f"{self._client.base_url}/entity/store/{store_id}"
        payload = self._client.get("/report/stock/all", params={"filter": f"store={href}", "limit": 1000})
        result: list[dict[str, Any]] = []
        while True:
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                raise ValueError("MoySklad stock report response has invalid rows")
            result.extend(row for row in rows if isinstance(row, dict))
            next_link = payload.get("meta", {}).get("nextHref") if isinstance(payload.get("meta"), dict) else None
            if not next_link:
                return result
            payload = self._client.get_url(str(next_link))

    def customer_order_by_external_code(self, external_code: str) -> dict[str, Any] | None:
        payload = self._client.get("/entity/customerorder", params={"filter": f"externalCode={external_code}", "limit": 1})
        rows = payload.get("rows", [])
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def create_customer_order(
        self,
        *,
        name: str,
        moment: str,
        organization_id: str,
        agent_id: str,
        store_id: str,
        external_code: str,
        positions: list[dict[str, Any]],
        description: str = "",
        sales_channel_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "moment": moment,
            "externalCode": external_code,
            "description": description,
            "organization": self._meta("organization", organization_id),
            "agent": self._meta("counterparty", agent_id),
            "store": self._meta("store", store_id),
            "positions": positions,
        }
        if sales_channel_id:
            body["salesChannel"] = self._meta("saleschannel", sales_channel_id)
        return self._client.post("/entity/customerorder", body)

    def update_customer_order_state(self, order_id: str, state_id: str) -> dict[str, Any]:
        """Move a customerorder to a workflow state (e.g. "Доставляется", "Выполнен").

        State ids are per-account custom workflow states, not a fixed enum —
        the meta href shape (.../customerorder/metadata/states/{id}) is fixed,
        but the id itself has to be looked up per account via GET
        /entity/customerorder/metadata; not reusable across MoySklad accounts.
        """
        body = {
            "state": {
                "meta": {
                    "href": f"{self._client.base_url}/entity/customerorder/metadata/states/{state_id}",
                    "type": "state",
                    "mediaType": "application/json",
                }
            }
        }
        return self._client.put(f"/entity/customerorder/{order_id}", body)

    def last_document_moment(self, entity: str, retail_store_id: str) -> str | None:
        """Most recent `moment` of a document (e.g. retaildemand) for one retail store, or None if there's none yet."""
        href = f"{self._client.base_url}/entity/retailstore/{retail_store_id}"
        payload = self._client.get(f"/entity/{entity}", params={"filter": f"retailStore={href}", "order": "moment,desc", "limit": 1})
        rows = payload.get("rows", [])
        return rows[0].get("moment") if rows and isinstance(rows[0], dict) else None

    def document_exists(self, entity: str, *, name: str, retail_store_id: str) -> bool:
        href = f"{self._client.base_url}/entity/retailstore/{retail_store_id}"
        payload = self._client.get(f"/entity/{entity}", params={"filter": f"name={name};retailStore={href}", "limit": 1})
        return bool(payload.get("rows"))

    def find_open_retail_shift(self, retail_store_id: str) -> dict[str, Any] | None:
        href = f"{self._client.base_url}/entity/retailstore/{retail_store_id}"
        payload = self._client.get("/entity/retailshift", params={"filter": f"retailStore={href};closeDate=", "order": "created,desc", "limit": 1})
        rows = payload.get("rows", [])
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def create_retail_shift(self, *, organization_id: str, store_id: str, retail_store_id: str, department_id: str, owner_id: str) -> dict[str, Any]:
        body = {
            "organization": self._meta("organization", organization_id),
            "store": self._meta("store", store_id),
            "retailStore": self._meta("retailstore", retail_store_id),
            "group": self._meta("group", department_id),
            "owner": self._meta("employee", owner_id),
        }
        return self._client.post("/entity/retailshift", body)

    def create_retail_demand(
        self,
        *,
        name: str,
        moment: str,
        document_number: int | None,
        check_number: str,
        organization_id: str,
        store_id: str,
        retail_store_id: str,
        retail_shift_id: str,
        department_id: str,
        owner_id: str,
        currency_id: str,
        positions: list[dict[str, Any]],
        cash_sum: int,
        non_cash_sum: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "moment": moment,
            "applicable": True,
            "checkNumber": check_number,
            "description": "",
            "cashSum": cash_sum,
            "noCashSum": non_cash_sum,
            "organization": self._meta("organization", organization_id),
            "store": self._meta("store", store_id),
            "retailStore": self._meta("retailstore", retail_store_id),
            "retailShift": self._meta("retailshift", retail_shift_id),
            "group": self._meta("group", department_id),
            "owner": self._meta("employee", owner_id),
            "rate": {"currency": self._meta("currency", currency_id)},
            "positions": positions,
        }
        if document_number is not None:
            body["documentNumber"] = document_number
        return self._client.post("/entity/retaildemand", body)

    def create_retail_return(
        self,
        *,
        name: str,
        moment: str,
        organization_id: str,
        store_id: str,
        retail_store_id: str,
        retail_shift_id: str,
        department_id: str,
        owner_id: str,
        currency_id: str,
        positions: list[dict[str, Any]],
        cash_sum: int,
        non_cash_sum: int,
    ) -> dict[str, Any]:
        body = {
            "name": name,
            "moment": moment,
            "description": "",
            "vatEnabled": True,
            "vatIncluded": True,
            "cashSum": cash_sum,
            "noCashSum": non_cash_sum,
            "organization": self._meta("organization", organization_id),
            "store": self._meta("store", store_id),
            "retailStore": self._meta("retailstore", retail_store_id),
            "retailShift": self._meta("retailshift", retail_shift_id),
            "group": self._meta("group", department_id),
            "owner": self._meta("employee", owner_id),
            "rate": {"currency": self._meta("currency", currency_id)},
            "positions": positions,
        }
        return self._client.post("/entity/retailsalesreturn", body)

    def open_retail_shifts(self, organization_id: str, since: datetime) -> list[dict[str, Any]]:
        """Retail shifts for an organization that have no closeDate yet.

        Scoped to shifts opened at or after ``since`` so we don't page through
        the full historical shift log (there can be well over 100k rows).
        """
        href = f"{self._client.base_url}/entity/organization/{organization_id}"
        since_str = since.strftime("%Y-%m-%d %H:%M:%S")
        payload = self._client.get(
            "/entity/retailshift",
            params={"filter": f"organization={href};moment>={since_str}", "order": "moment,asc", "limit": 100, "expand": "retailStore"},
        )
        result: list[dict[str, Any]] = []
        while True:
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                raise ValueError("MoySklad retailshift response has invalid rows")
            result.extend(row for row in rows if isinstance(row, dict) and not row.get("closeDate"))
            next_link = payload.get("meta", {}).get("nextHref") if isinstance(payload.get("meta"), dict) else None
            if not next_link:
                return result
            payload = self._client.get_url(str(next_link))

    def close_retail_shift(self, shift_id: str, close_date: str) -> None:
        self._client.put(f"/entity/retailshift/{shift_id}", {"closeDate": close_date})

    def close(self) -> None:
        self._client.close()
