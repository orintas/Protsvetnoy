from __future__ import annotations

from typing import Any

from .http import JsonClient


class MakeClient:
    """Read-only access to Make.com scenarios (blueprints), for inspecting
    existing automations before porting them into this service."""

    def __init__(self, *, base_url: str, token: str) -> None:
        self._client = JsonClient(base_url=base_url, headers={"Authorization": f"Token {token}"})

    def get_blueprint(self, scenario_id: int) -> dict[str, Any]:
        payload = self._client.get(f"/scenarios/{scenario_id}/blueprint")
        response = payload.get("response")
        if isinstance(response, dict) and isinstance(response.get("blueprint"), dict):
            return response["blueprint"]
        if isinstance(payload.get("blueprint"), dict):
            return payload["blueprint"]
        return payload

    def datastore_records(self, datastore_id: int, *, limit: int = 1000) -> list[dict[str, Any]]:
        payload = self._client.get(f"/data-stores/{datastore_id}/data", params={"pg[limit]": limit})
        records = payload.get("records", [])
        return [r for r in records if isinstance(r, dict)]

    def list_scenarios(self, team_id: int | None = None) -> list[dict[str, Any]]:
        params = {"teamId": team_id} if team_id is not None else None
        payload = self._client.get("/scenarios", params=params)
        scenarios = payload.get("scenarios", [])
        return [s for s in scenarios if isinstance(s, dict)]

    def close(self) -> None:
        self._client.close()
