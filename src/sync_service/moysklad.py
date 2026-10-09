from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from .change_log import record
from .http import JsonClient

# The "ProTsvetnoy OU" group — the org's non-Russian retail arm. Product
# categories live under this group; Russia-side categories are excluded.
PROTSVETNOY_GROUP_ID = "62a11082-1b25-11ea-0a80-030300038a2c"

# Contact-sync (PlanFix) destination groups, keyed by which PlanFix
# "Отдел" (department) a contact belongs to — ported as-is from the Make
# scenario it replaces.
PLANFIX_DEPARTMENT_GROUPS = {
    "Varvikas": PROTSVETNOY_GROUP_ID,
    "Цветной": "08e6b024-d269-11e4-90a2-8ecb0004d9d2",
}


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

    def products_pages(self) -> Iterator[list[dict[str, Any]]]:
        """Yield each page of /entity/product as it's fetched, rather than
        buffering the whole catalog — lets a caller report progress on a
        fetch that otherwise runs silently for tens of seconds."""
        payload = self._client.get("/entity/product", params={"limit": 1000})
        while True:
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                raise ValueError("MoySklad products response has invalid rows")
            yield [row for row in rows if isinstance(row, dict)]
            next_link = payload.get("meta", {}).get("nextHref") if isinstance(payload.get("meta"), dict) else None
            if not next_link:
                return
            payload = self._client.get_url(str(next_link))

    def products(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in self.products_pages():
            result.extend(page)
        return result

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

    def stock_by_products(self, product_ids: list[str], *, store_id: str) -> list[dict[str, Any]]:
        """Sellable stock (stock/reserve/quantity, plus "code"/"article") for
        specific products at one store — a narrower, cheaper version of
        stock_by_store for a handful of known SKUs (e.g. answering a stock
        question about one order's positions instead of the whole warehouse)."""
        href = f"{self._client.base_url}/entity/store/{store_id}"
        filter_value = ";".join([f"product={self._client.base_url}/entity/product/{pid}" for pid in product_ids] + [f"store={href}"])
        payload = self._client.get("/report/stock/all", params={"filter": filter_value, "limit": 1000})
        rows = payload.get("rows", [])
        return [row for row in rows if isinstance(row, dict)]

    def customer_order_positions(self, order_id: str) -> list[dict[str, Any]]:
        payload = self._client.get(f"/entity/customerorder/{order_id}/positions", params={"limit": 1000, "expand": "assortment"})
        rows = payload.get("rows", [])
        return [row for row in rows if isinstance(row, dict)]

    def create_loss(self, *, organization_id: str, store_id: str, positions: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        """Списание — a stock write-off. `positions` items are shaped
        `{"quantity": ..., "assortment": {"meta": ...}}` (e.g. taken straight
        from customer_order_positions' expanded assortment). `name` is left
        for MoySklad to auto-assign, same convention as create_customer_order."""
        body: dict[str, Any] = {
            "organization": self._meta("organization", organization_id),
            "store": self._meta("store", store_id),
            "positions": positions,
            "description": description,
        }
        result = self._client.post("/entity/loss", body)
        record(service="moysklad", entity_type="loss", entity_id=result.get("name") or "", action="create",
               after={"store_id": store_id, "positions": len(positions), "description": description}, link=result.get("meta", {}).get("uuidHref"))
        return result

    def customer_order_by_external_code(self, external_code: str) -> dict[str, Any] | None:
        payload = self._client.get("/entity/customerorder", params={"filter": f"externalCode={external_code}", "limit": 1})
        rows = payload.get("rows", [])
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def customer_order_by_name(self, name: str) -> dict[str, Any] | None:
        """For orders created by some other integration, keyed by document
        name rather than externalCode (e.g. OZON's own MoySklad integration
        names each customerorder after the posting_number)."""
        payload = self._client.get("/entity/customerorder", params={"filter": f"name={name}", "limit": 1})
        rows = payload.get("rows", [])
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def update_customer_order_description(self, order_id: str, description: str, *, previous_description: str | None = None) -> dict[str, Any]:
        result = self._client.put(f"/entity/customerorder/{order_id}", {"description": description})
        record(service="moysklad", entity_type="customerorder.description", entity_id=result.get("name") or order_id, action="update",
               before=previous_description, after=description, link=result.get("meta", {}).get("uuidHref"))
        return result

    def _state_meta(self, state_id: str) -> dict[str, Any]:
        return {
            "meta": {
                "href": f"{self._client.base_url}/entity/customerorder/metadata/states/{state_id}",
                "type": "state",
                "mediaType": "application/json",
            }
        }

    def create_customer_order(
        self,
        *,
        name: str | None = None,
        moment: str,
        organization_id: str,
        agent_id: str,
        store_id: str,
        external_code: str,
        positions: list[dict[str, Any]],
        description: str = "",
        sales_channel_id: str | None = None,
        currency_id: str | None = None,
        state_id: str | None = None,
        owner_id: str | None = None,
        group_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """`name=None` lets MoySklad assign the next number in its own shared
        sequence — the convention already used for every manually-entered
        customer order in this account, Shopify included."""
        body: dict[str, Any] = {
            "moment": moment,
            "externalCode": external_code,
            "description": description,
            "organization": self._meta("organization", organization_id),
            "agent": self._meta("counterparty", agent_id),
            "store": self._meta("store", store_id),
            "positions": positions,
        }
        if name is not None:
            body["name"] = name
        if sales_channel_id:
            body["salesChannel"] = self._meta("saleschannel", sales_channel_id)
        if currency_id:
            body["rate"] = {"currency": self._meta("currency", currency_id)}
        if state_id:
            body["state"] = self._state_meta(state_id)
        if owner_id:
            body["owner"] = self._meta("employee", owner_id)
        if group_id:
            body["group"] = self._meta("group", group_id)
        if project_id:
            body["project"] = self._meta("project", project_id)
        result = self._client.post("/entity/customerorder", body)
        record(service="moysklad", entity_type="customerorder", entity_id=result.get("name") or external_code, action="create",
               after={"name": result.get("name"), "externalCode": external_code, "positions": len(positions), "description": description, "state_id": state_id, "currency_id": currency_id, "project_id": project_id},
               link=result.get("meta", {}).get("uuidHref"))
        return result

    def update_customer_order_state(self, order_id: str, state_id: str, *, previous_state_id: str | None = None) -> dict[str, Any]:
        """Move a customerorder to a workflow state (e.g. "Доставляется", "Выполнен").

        State ids are per-account custom workflow states, not a fixed enum —
        the meta href shape (.../customerorder/metadata/states/{id}) is fixed,
        but the id itself has to be looked up per account via GET
        /entity/customerorder/metadata; not reusable across MoySklad accounts.
        """
        result = self._client.put(f"/entity/customerorder/{order_id}", {"state": self._state_meta(state_id)})
        record(service="moysklad", entity_type="customerorder.state", entity_id=result.get("name") or order_id, action="update",
               before=previous_state_id, after=state_id, link=result.get("meta", {}).get("uuidHref"))
        return result

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
        result = self._client.post("/entity/retailshift", body)
        record(service="moysklad", entity_type="retailshift", entity_id=result.get("name") or str(result.get("id")), action="create",
               after={"retail_store_id": retail_store_id, "store_id": store_id}, link=result.get("meta", {}).get("uuidHref"))
        return result

    def create_retail_demand(
        self,
        *,
        name: str,
        moment: str,
        document_number: int | None,
        check_number: int | None,
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
        if check_number is not None:
            body["checkNumber"] = check_number
        result = self._client.post("/entity/retaildemand", body)
        record(service="moysklad", entity_type="retaildemand", entity_id=name, action="create",
               after={"cash_sum": cash_sum, "non_cash_sum": non_cash_sum, "positions": len(positions), "retail_shift_id": retail_shift_id},
               link=result.get("meta", {}).get("uuidHref"))
        return result

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
        result = self._client.post("/entity/retailsalesreturn", body)
        record(service="moysklad", entity_type="retailsalesreturn", entity_id=name, action="create",
               after={"cash_sum": cash_sum, "non_cash_sum": non_cash_sum, "positions": len(positions), "retail_shift_id": retail_shift_id},
               link=result.get("meta", {}).get("uuidHref"))
        return result

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

    def retail_shift_close_date(self, shift_id: str) -> str | None:
        """Re-read one shift's closeDate right before closing it — the nightly
        sweep's own "open shifts" list can go stale between listing and acting
        if the store's own POS (e.g. Касса МойСклад) closes the same shift in
        the meantime; closing an already-closed shift fails with a confusing
        "name" uniqueness error (code 3006) rather than a clear "already closed"."""
        payload = self._client.get(f"/entity/retailshift/{shift_id}")
        return payload.get("closeDate")

    def close_retail_shift(self, shift_id: str, close_date: str, *, name: str | None = None) -> None:
        """`name` is only ever passed to work around a live-confirmed MoySklad
        quirk: closing a shift can be rejected for a "name" uniqueness
        conflict with another shift in the same store, even though closeDate
        is the only field actually being changed — retrying under a
        suffixed name (e.g. "00265-1") is the caller's fallback for that."""
        body: dict[str, Any] = {"closeDate": close_date}
        if name is not None:
            body["name"] = name
        result = self._client.put(f"/entity/retailshift/{shift_id}", body)
        record(service="moysklad", entity_type="retailshift.closeDate", entity_id=result.get("name") or name or shift_id, action="update",
               before=None, after=close_date, link=result.get("meta", {}).get("uuidHref"))

    def counterparty_by_email(self, email: str) -> dict[str, Any] | None:
        """Safety net for the PlanFix contact sync: a match here means a
        counterparty for this email already exists even if PlanFix's own
        stored MoySkladID is missing or stale — avoids creating a duplicate."""
        if not email:
            return None
        payload = self._client.get("/entity/counterparty", params={"filter": f"email={email}", "limit": 1})
        rows = payload.get("rows", [])
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def create_counterparty(self, *, name: str, email: str, phone: str, group_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "email": email, "phone": phone, "group": self._meta("group", group_id)}
        result = self._client.post("/entity/counterparty", body)
        record(service="moysklad", entity_type="counterparty", entity_id=name, action="create",
               after={"name": name, "email": email, "phone": phone}, link=result.get("meta", {}).get("uuidHref"))
        return result

    def update_counterparty(self, counterparty_id: str, *, name: str, email: str, phone: str, group_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "email": email, "phone": phone, "group": self._meta("group", group_id)}
        result = self._client.put(f"/entity/counterparty/{counterparty_id}", body)
        record(service="moysklad", entity_type="counterparty", entity_id=name, action="update",
               after={"name": name, "email": email, "phone": phone}, link=result.get("meta", {}).get("uuidHref"))
        return result

    def counterparty_report(self, counterparty_id: str) -> dict[str, Any] | None:
        """Sales stats (firstDemandDate, lastDemandDate, demandsCount,
        demandsSum) — also doubles as "does this id still exist" (None on a
        404, e.g. the counterparty was deleted after PlanFix cached its id)."""
        return self._client.get_optional(f"/report/counterparty/{counterparty_id}")

    def demand_template_from_customer_order(self, order_id: str) -> dict[str, Any]:
        """Pre-filled Отгрузка draft copied from a Заказ покупателя (same as
        clicking "Создать документ → Отгрузка" on the order in the web UI) —
        organization, agent, store and positions all come from the order."""
        return self._client.put("/entity/demand/new", {"customerOrder": self._meta("customerorder", order_id)})

    def create_demand(self, template: dict[str, Any]) -> dict[str, Any]:
        return self._client.post("/entity/demand", template)

    def demand_positions(self, demand_id: str) -> list[dict[str, Any]]:
        payload = self._client.get(f"/entity/demand/{demand_id}/positions", params={"limit": 1000})
        rows = payload.get("rows", [])
        return [row for row in rows if isinstance(row, dict)]

    def stock_by_slot(self, assortment_ids: list[str], *, store_id: str) -> list[dict[str, Any]]:
        """Current stock per ячейка for the given products at one warehouse:
        [{"assortmentId", "storeId", "slotId", "stock"}, ...]. Products with
        no address-storage stock at all (service items, non-stored goods)
        simply don't appear in the result."""
        filter_value = ";".join([f"assortmentId={aid}" for aid in assortment_ids] + [f"storeId={store_id}"])
        return self._client.get_array("/report/stock/byslot/current", params={"filter": filter_value})

    def set_position_slot(self, demand_id: str, position_id: str, *, store_id: str, slot_id: str) -> dict[str, Any]:
        body = {"slot": {"meta": {"href": f"{self._client.base_url}/entity/store/{store_id}/slots/{slot_id}", "type": "slot", "mediaType": "application/json"}}}
        return self._client.put(f"/entity/demand/{demand_id}/positions/{position_id}", body)

    def close(self) -> None:
        self._client.close()
