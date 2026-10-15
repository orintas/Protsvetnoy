from __future__ import annotations

from typing import Any

from .http import JsonClient

# System fields always requested on a contact lookup, on top of whatever
# custom field ids the caller asks for.
_CONTACT_SYSTEM_FIELDS = ("id", "name", "email", "phones", "isCompany")


class PlanFixClient:
    """REST API v2 (Bearer-token auth)."""

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self._client = JsonClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        self._contact_field_ids: dict[str, str] | None = None

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._client.get(path, params=params)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._client.post(path, payload)

    def get_contact(self, contact_id: str, *, extra_field_ids: list[str] = ()) -> dict[str, Any]:
        """A contact/company by id, with its customFieldData for whichever
        custom field ids were asked for (resolve names to ids first via
        contact_custom_field_id).

        The id is sent with an explicit "contact:" prefix — PlanFix's docs
        say a bare number works too ("contact:12 OR 12"), but a bare number
        came back "Contact not found" live against this account (confirmed
        2026-10-02) for an id that was in fact a real contact, presumably
        because PlanFix's object ids aren't unique per entity type without
        the prefix to disambiguate.
        """
        prefixed_id = contact_id if contact_id.startswith("contact:") else f"contact:{contact_id}"
        fields = ",".join((*_CONTACT_SYSTEM_FIELDS, *extra_field_ids))
        payload = self._client.get(f"/contact/{prefixed_id}", params={"fields": fields})
        return payload["contact"]

    def contact_custom_field_id(self, field_name: str) -> str | None:
        """Resolves a PlanFix contact custom field's display name (e.g.
        "MoySkladID") to its numeric id, needed to ask for it via `fields=`
        on get_contact. Cached per client instance — the field list doesn't
        change within one webhook handling."""
        if self._contact_field_ids is None:
            # Like /contact/{id}, this endpoint only returns the bare id for
            # each field unless "fields" is passed explicitly — confirmed
            # live 2026-10-02: without it every entry was just {"id": N},
            # which is why every field name (and the MoySkladID lookup)
            # resolved to nothing even after fixing the "customfields"
            # (lowercase) response-key bug.
            payload = self._client.get("/customfield/contact", params={"fields": "id,name"})
            fields = payload.get("customfields", [])
            self._contact_field_ids = {f["name"]: str(f["id"]) for f in fields if isinstance(f, dict) and "name" in f and "id" in f}
        return self._contact_field_ids.get(field_name)

    def get_task(self, task_id: int, *, field_ids: list[int] = ()) -> dict[str, Any]:
        """A task by id, with system fields plus whichever custom field ids
        were asked for. A custom field's resolved value comes back under
        `customFieldData` — a list of `{"field": {"id", "name", "type", ...},
        "stringValue", "value"}` entries, one per field that was both
        requested AND actually has a value set. For a "Directory entry"
        field (type 9) `value` is `{"id": <directory entry key>, "value":
        <display text>}`; for "Set of directory values" (type 15) it's
        presumably the same shape per element of an array — confirmed live
        for the single-entry case (2026-10-15), not yet for the array one,
        since every real task found already had it cleared by the time it
        was inspected; verify against the first live webhook delivery.

        Like get_contact, custom field ids must be requested by their own
        bare numeric id in `fields=` — there's no generic "customData"/
        "customFieldData" keyword that pulls all of them at once.
        """
        fields = ",".join(("id", "name", "parent", "project", *(str(f) for f in field_ids)))
        payload = self._client.get(f"/task/{task_id}", params={"fields": fields})
        return payload["task"]

    def create_task(
        self,
        *,
        name: str,
        template_id: int | None = None,
        parent_id: int | None = None,
        project_id: int | None = None,
        description: str = "",
        custom_data: list[dict[str, Any]] | None = None,
    ) -> int:
        """Creates a task, returns its new id — lands in the template's
        default initial status (confirmed live 2026-10-16: "Новая"/New).

        `custom_data`: `[{"field": {"id": <field id>}, "value": {"id": <directory
        entry key>}}, ...]` for Directory-entry-typed fields (the only kind
        this service sets).

        There used to be a `status` parameter here that sent a bare string
        (e.g. "ACCEPTED") — removed after confirming live that POST /task/
        rejects that outright ("Cannot deserialize, invalid JSON"): PlanFix
        only accepts a {"id": int} status reference, and even then only
        DRAFT/ACTIVE/DELAYED are valid to set at creation time — not
        ACCEPTED/COMPLETED/etc. Setting a task to one of those after
        creation needs a separate status-change call this client doesn't
        have yet.

        Endpoint/body shape otherwise ported from a published, actively-used
        community REST v2 client (github.com/popstas/planfix-mcp-server,
        src/tools/planfix_create_sell_task_ids.ts).
        """
        body: dict[str, Any] = {"name": name, "description": description}
        if template_id is not None:
            body["template"] = {"id": template_id}
        if parent_id is not None:
            body["parent"] = {"id": parent_id}
        if project_id is not None:
            body["project"] = {"id": project_id}
        if custom_data:
            body["customFieldData"] = custom_data
        payload = self._client.post("/task/", body)
        return int(payload["id"])

    def close(self) -> None:
        self._client.close()
