from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


class ApiError(RuntimeError):
    """Raised when a remote API returns an unsuccessful response."""


class JsonClient:
    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers=dict(headers or {}),
            auth=auth,
            timeout=timeout,
        )

    @property
    def base_url(self) -> str:
        return str(self._client.base_url).rstrip("/")

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
    ) -> dict[str, Any]:
        response = self._client.get(path, params=params)
        if response.is_error:
            raise ApiError(f"GET {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ApiError(f"GET {response.url} returned a non-object JSON response")
        return payload

    def get_url(self, url: str) -> dict[str, Any]:
        response = self._client.get(url)
        if response.is_error:
            raise ApiError(f"GET {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ApiError(f"GET {response.url} returned a non-object JSON response")
        return payload

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self._client.post(path, json=payload)
        if response.is_error:
            raise ApiError(f"POST {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        result = response.json()
        if not isinstance(result, dict):
            raise ApiError(f"POST {response.url} returned a non-object JSON response")
        return result

    def close(self) -> None:
        self._client.close()
