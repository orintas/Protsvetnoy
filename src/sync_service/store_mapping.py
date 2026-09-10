from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoreMapping:
    novicloud_store_id: int
    moysklad_store_id: str
    name: str
    moysklad_store_href: str


def load_store_mappings(path: Path | str = "data/store-mappings.json") -> tuple[StoreMapping, ...]:
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("Store mapping file must contain a JSON array")
    return tuple(StoreMapping(**record) for record in records)


def find_by_novicloud_store_id(
    store_id: int,
    mappings: tuple[StoreMapping, ...],
) -> StoreMapping:
    for mapping in mappings:
        if mapping.novicloud_store_id == store_id:
            return mapping
    raise KeyError(f"No MoySklad mapping for Novicloud store ID {store_id}")
