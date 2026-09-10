from __future__ import annotations

from datetime import datetime
from typing import Any

from .http import JsonClient


class NovicloudClient:
    def __init__(self, *, base_url: str, version: str, account: str, password: str) -> None:
        self._client = JsonClient(
            base_url=f"{base_url}/{version}/{account}",
            auth=(account, password),
        )

    def products(self, *, barcode: str | None = None) -> dict[str, Any]:
        params = {"kod": barcode} if barcode else None
        return self._client.get("/towary", params=params)

    def stores(self) -> dict[str, Any]:
        return self._client.get("/sklepy")

    def sales(self, *, date_from: datetime | None = None, date_to: datetime | None = None) -> dict[str, Any]:
        params: list[tuple[str, str]] = []
        if date_from:
            params.append(("data", f"min{date_from.isoformat(timespec='seconds')}"))
        if date_to:
            params.append(("data", f"max{date_to.isoformat(timespec='seconds')}"))
        return self._client.get("/sprzedaz", params=params or None)

    def stocks(self, *, date: str | None = None) -> dict[str, Any]:
        params = {"na_dzien": date} if date else None
        return self._client.get("/stanymag", params=params)

    def close(self) -> None:
        self._client.close()
