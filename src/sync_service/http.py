from __future__ import annotations

import time
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
        max_retries: int = 3,
        retry_backoff: float = 1.5,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers=dict(headers or {}),
            auth=auth,
            timeout=timeout,
        )
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff

    @property
    def base_url(self) -> str:
        return str(self._client.base_url).rstrip("/")

    def _request_with_retry(self, method: str, url_or_path: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.request(method, url_or_path, **kwargs)
            except httpx.TransportError:
                # Network-level failure (read/connect timeout, connection reset) rather than
                # an HTTP response — retry the same way as a 5xx, since it's just as transient.
                if attempt <= self._max_retries:
                    time.sleep(self._retry_backoff * attempt)
                    continue
                raise
            # Retry only on transient server-side errors (5xx); client errors (4xx) are final.
            if response.status_code >= 500 and attempt <= self._max_retries:
                time.sleep(self._retry_backoff * attempt)
                continue
            return response

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
    ) -> dict[str, Any]:
        response = self._request_with_retry("GET", path, params=params)
        if response.is_error:
            raise ApiError(f"GET {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ApiError(f"GET {response.url} returned a non-object JSON response")
        return payload

    def get_bytes(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
    ) -> bytes:
        """For endpoints returning a binary body (e.g. a PDF label) rather than JSON."""
        response = self._request_with_retry("GET", path, params=params)
        if response.is_error:
            raise ApiError(f"GET {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        return response.content

    def post_bytes(self, path: str, payload: Mapping[str, Any]) -> bytes:
        """For endpoints that take a JSON body but return a binary response
        body (e.g. a PDF label) rather than JSON."""
        response = self._request_with_retry("POST", path, json=payload)
        if response.is_error:
            raise ApiError(f"POST {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        return response.content

    def get_bytes_url(self, url: str) -> bytes:
        response = self._request_with_retry("GET", url)
        if response.is_error:
            raise ApiError(f"GET {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        return response.content

    def get_url(self, url: str) -> dict[str, Any]:
        response = self._request_with_retry("GET", url)
        if response.is_error:
            raise ApiError(f"GET {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ApiError(f"GET {response.url} returned a non-object JSON response")
        return payload

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request_with_retry("POST", path, json=payload)
        if response.is_error:
            raise ApiError(f"POST {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        result = response.json()
        if not isinstance(result, dict):
            raise ApiError(f"POST {response.url} returned a non-object JSON response")
        return result

    def put(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request_with_retry("PUT", path, json=payload)
        if response.is_error:
            raise ApiError(f"PUT {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        result = response.json()
        if not isinstance(result, dict):
            raise ApiError(f"PUT {response.url} returned a non-object JSON response")
        return result

    def post_with_query(self, path: str, *, params: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
        """Some endpoints take pagination as query params but still require a JSON body."""
        response = self._request_with_retry("POST", path, params=params, json=body)
        if response.is_error:
            raise ApiError(f"POST {response.url} failed with HTTP {response.status_code}: {response.text[:500]}")
        result = response.json()
        if not isinstance(result, dict):
            raise ApiError(f"POST {response.url} returned a non-object JSON response")
        return result

    def close(self) -> None:
        self._client.close()
