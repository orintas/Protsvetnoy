from __future__ import annotations

import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import certifi

from .http import JsonClient

# T-Bank Business T-API (developer.tbank.ru). The sandbox lives under
# /openapi/sandbox with the fixed token "TBankSandboxToken" — response
# shapes below were confirmed against it on 2026-11-11.
DEFAULT_BASE_URL = "https://business.tbank.ru/openapi"
SANDBOX_BASE_URL = "https://business.tbank.ru/openapi/sandbox"

# Both trade-acquiring methods are limited to 1 request per second.
ACQUIRING_RATE_LIMIT_SECONDS = 1.1
# One /tacq/operations call may cover at most one day and 1000 operations.
OPERATIONS_LIMIT = 1000


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# business.tbank.ru is signed by the Russian Ministry of Digital Development's
# root CA, which certifi (and python:3.12-slim) doesn't ship. Downloaded from
# gu-st.ru on 2026-11-11; SHA-256 D2:6D:2D:02:…:CA:8E:CF:31, matching both the
# chain T-Bank serves and the copy in the macOS keychain. Expires 2032-02-27.
RUSSIAN_ROOT_CA = Path(__file__).resolve().parents[2] / "config" / "certs" / "russian_trusted_root_ca.pem"


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=certifi.where())
    for candidate in (RUSSIAN_ROOT_CA, Path("config/certs/russian_trusted_root_ca.pem")):
        if candidate.exists():
            context.load_verify_locations(cafile=str(candidate))
            break
    return context


class TBankClient:
    def __init__(self, *, token: str, base_url: str = DEFAULT_BASE_URL) -> None:
        self._client = JsonClient(base_url=base_url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                  verify=_ssl_context())
        self._last_acquiring_call = 0.0

    def close(self) -> None:
        self._client.close()

    def _acquiring_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        wait = ACQUIRING_RATE_LIMIT_SECONDS - (time.monotonic() - self._last_acquiring_call)
        if wait > 0:
            time.sleep(wait)
        try:
            return self._client.get(path, params=params)
        finally:
            self._last_acquiring_call = time.monotonic()

    def terminals(self) -> list[dict[str, Any]]:
        """Every active trade-acquiring terminal: [{"key": serial number, "id": terminal id}, ...]."""
        result: list[dict[str, Any]] = []
        page = 0
        while True:
            payload = self._acquiring_get("/api/v1/tacq/terminals", {"page": page, "size": 100})
            result.extend(row for row in payload.get("terminals") or [] if isinstance(row, dict))
            if payload.get("last", True) or page + 1 >= int(payload.get("totalPages") or 0):
                return result
            page += 1

    def terminal_operations(self, terminal_key: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Card operations of one terminal in [start, end) — at most one day.

        Each row: rrn, transactionDate (UTC), amount (kopecks), cardNumber
        (masked), type ("Debit" = payment, "Credit" = refund)."""
        payload = self._acquiring_get(
            f"/api/v1/tacq/operations/terminal/{terminal_key}",
            # `till` is inclusive and the window may not exceed one day —
            # confirmed live: 00:00..00:00 next day is rejected (VALIDATION_FAILED).
            {"from": _utc(start), "till": _utc(end - timedelta(seconds=1)), "limit": OPERATIONS_LIMIT},
        )
        rows = [row for row in payload.get("operations") or [] if isinstance(row, dict)]
        if len(rows) >= OPERATIONS_LIMIT:
            raise RuntimeError(f"Терминал {terminal_key}: {len(rows)} операций за период — достигнут предел T-API, нужен период короче")
        return rows

    def statement(self, account_number: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Every booked operation on the account in [start, end), following nextCursor."""
        params: dict[str, Any] = {
            "accountNumber": account_number,
            "from": _utc(start),
            "to": _utc(end),
            "limit": 5000,
            "operationStatus": "Transaction",
        }
        result: list[dict[str, Any]] = []
        while True:
            payload = self._client.get("/api/v1/statement", params=params)
            result.extend(row for row in payload.get("operations") or [] if isinstance(row, dict))
            cursor = payload.get("nextCursor")
            if not cursor:
                return result
            params = {**params, "cursor": cursor}
