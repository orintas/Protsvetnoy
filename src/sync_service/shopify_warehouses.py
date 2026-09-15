from __future__ import annotations

import json
from pathlib import Path

from .moysklad import MoySkladClient

# MoySklad organization id -> country name, for grouping warehouse choices in
# the picker UI. Same four organizations the shift-closer already targets.
COUNTRY_ORGANIZATIONS: dict[str, str] = {
    "a623f6a9-dde8-11ed-0a80-01540011a4a0": "Польша",
    "147a9f38-1896-11ed-0a80-0e5d000a5470": "Литва",
    "94ad58fb-beec-11ec-0a80-092400291358": "Латвия",
    "0b3fbc43-e0dc-11ec-0a80-0076000f9c69": "Эстония",
}

# General (non-shop) warehouses that don't belong to any single mall retail
# store, so they don't show up via COUNTRY_ORGANIZATIONS — confirmed live as
# active, non-archived, with real stock. The user specifically named
# "Protsvetnoy" as Estonia's main warehouse. "Основной склад" is excluded on
# purpose — the user asked for it to never be part of the Shopify stock sync.
GENERAL_WAREHOUSES: list[dict[str, str]] = [
    {"id": "d9a80084-1bf4-11ea-0a80-057b000493d9", "name": "ProTsvetnoy OU"},
]


def available_warehouses(moysklad: MoySkladClient) -> list[dict[str, str]]:
    """All warehouse choices for the picker, grouped by country label."""
    result: list[dict[str, str]] = []
    for org_id, country in COUNTRY_ORGANIZATIONS.items():
        for wh in moysklad.warehouses_by_organization(org_id):
            result.append({"id": wh["id"], "name": wh["name"], "country": country})
    for wh in GENERAL_WAREHOUSES:
        result.append({"id": wh["id"], "name": wh["name"], "country": "Общие склады"})
    return result


class ShopifyWarehouseConfig:
    """Which MoySklad warehouses feed the Shopify stock sync — a human picks
    them once via the web UI; there's no way to derive this automatically."""

    def __init__(self, path: str = "data/shopify-warehouses.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[str]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []

    def save(self, warehouse_ids: list[str]) -> list[str]:
        selection = sorted({str(x) for x in warehouse_ids})
        self.path.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
        return selection
