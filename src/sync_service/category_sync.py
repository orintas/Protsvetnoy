from __future__ import annotations

import json
from pathlib import Path

# The six categories that were hardcoded for Novicloud sync before this page
# existed — used as the seed selection so shipping this feature doesn't
# silently change what gets synced on day one.
DEFAULT_NOVICLOUD_CATEGORIES = (
    "Painting by numbers", "Diamond painting", "Products for Shops", "Accessories",
    "Wooden constructors", "Roombox",
)


class CategorySyncConfig:
    """Which MoySklad categories (ProTsvetnoy OU group) sync to which channel.

    Persisted as a small JSON file rather than SQLite since it's a handful of
    checkboxes a human edits occasionally, not an event log.
    """

    def __init__(self, path: str = "data/category-sync.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {"novicloud": list(DEFAULT_NOVICLOUD_CATEGORIES), "shopify": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {
            "novicloud": [str(name) for name in data.get("novicloud", [])],
            "shopify": [str(name) for name in data.get("shopify", [])],
        }

    def save(self, selection: dict[str, list[str]]) -> dict[str, list[str]]:
        payload = {
            "novicloud": sorted({str(name) for name in selection.get("novicloud", [])}),
            "shopify": sorted({str(name) for name in selection.get("shopify", [])}),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
