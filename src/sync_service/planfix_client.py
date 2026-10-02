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
        contact_custom_field_id)."""
        fields = ",".join((*_CONTACT_SYSTEM_FIELDS, *extra_field_ids))
        payload = self._client.get(f"/contact/{contact_id}", params={"fields": fields})
        return payload["contact"]

    def contact_custom_field_id(self, field_name: str) -> str | None:
        """Resolves a PlanFix contact custom field's display name (e.g.
        "MoySkladID") to its numeric id, needed to ask for it via `fields=`
        on get_contact. Cached per client instance — the field list doesn't
        change within one webhook handling."""
        if self._contact_field_ids is None:
            payload = self._client.get("/customfield/contact")
            fields = payload.get("customFields", payload.get("fields", []))
            self._contact_field_ids = {f["name"]: str(f["id"]) for f in fields if isinstance(f, dict) and "name" in f and "id" in f}
        return self._contact_field_ids.get(field_name)

    def close(self) -> None:
        self._client.close()
