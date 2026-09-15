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

    def all_products(self) -> list[dict[str, Any]]:
        payload = self.products()
        result: list[dict[str, Any]] = []
        while True:
            rows = payload.get("dane", [])
            if not isinstance(rows, list):
                raise ValueError("Novicloud products response has invalid dane")
            result.extend(row for row in rows if isinstance(row, dict))
            next_link = payload.get("links", {}).get("next") if isinstance(payload.get("links"), dict) else None
            if not next_link:
                return result
            payload = self._client.get_url(str(next_link))

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

    def documents(self, *, typ_dok: str, sklep_id: int, date_from: str | None = None) -> dict[str, Any]:
        """Retail documents (`/dokumenty`) for one store, optionally since a date.

        typ_dok: comma-separated Novicloud document type codes (21,112 = retail
        sale receipts, 8 = returns). date_from: "YYYY-MM-DDTHH:MM:SS", sent as
        `data_wystawienia=min<date_from>` (matches production usage confirmed
        against the live API).
        """
        params: list[tuple[str, str]] = [("typ_dok", typ_dok), ("sklep.id", str(sklep_id))]
        if date_from:
            params.append(("data_wystawienia", f"min{date_from}"))
        return self._client.get("/dokumenty", params=params)

    def get_url(self, url: str) -> dict[str, Any]:
        """Follow an absolute link returned by the API (e.g. a document's positions or a product)."""
        return self._client.get_url(url)

    def close(self) -> None:
        self._client.close()
