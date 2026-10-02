from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .moysklad import MoySkladClient


@dataclass
class UnresolvedPosition:
    assortment_id: str
    needed: float
    available: float


@dataclass
class DemandSlotResult:
    order_id: str
    demand_id: str
    demand_number: str
    assigned: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[UnresolvedPosition] = field(default_factory=list)


def _id_from_href(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]


def create_demands_with_slots(moysklad: MoySkladClient, order_ids: list[str]) -> list[DemandSlotResult]:
    """Create one Отгрузка per given Заказ покупателя and assign every line a
    ячейка — the same outcome as clicking "Создать документ → Отгрузка" on
    each order and then "Подобрать ячейки" on it, just without opening each
    document by hand.

    MoySklad's own "Подобрать ячейки" has no documented API endpoint (not
    found anywhere in the public JSON API docs) and, from testing it live,
    does nothing cleverer than: use the one cell holding the product if
    there's only one, otherwise pick among whichever cells hold enough. This
    reproduces that using the public stock-by-slot report, preferring the
    fullest matching cell first so a batch of several orders concentrates on
    fewer cells rather than spreading thin across many.

    Cells are tracked against a running tally that's decremented as each
    position is assigned, not just read once up front — otherwise two
    positions in this same batch needing the same product could each be
    told the same cell has enough, when combined it doesn't.
    """
    created: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    for order_id in order_ids:
        template = moysklad.demand_template_from_customer_order(order_id)
        demand = moysklad.create_demand(template)
        positions = moysklad.demand_positions(demand["id"])
        created.append((order_id, demand, positions))

    stock_by_store: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for _, demand, positions in created:
        store_id = _id_from_href(demand["store"]["meta"]["href"])
        if store_id in stock_by_store:
            continue
        assortment_ids = sorted({_id_from_href(p["assortment"]["meta"]["href"]) for p in positions})
        if not assortment_ids:
            continue
        rows = moysklad.stock_by_slot(assortment_ids, store_id=store_id)
        by_assortment: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_assortment.setdefault(row["assortmentId"], []).append(dict(row))
        for slots in by_assortment.values():
            slots.sort(key=lambda r: r["stock"], reverse=True)
        stock_by_store[store_id] = by_assortment

    results: list[DemandSlotResult] = []
    for order_id, demand, positions in created:
        store_id = _id_from_href(demand["store"]["meta"]["href"])
        available = stock_by_store.get(store_id, {})
        result = DemandSlotResult(order_id=order_id, demand_id=demand["id"], demand_number=demand.get("name", ""))
        for position in positions:
            assortment_id = _id_from_href(position["assortment"]["meta"]["href"])
            needed = position["quantity"]
            candidates = available.get(assortment_id, [])
            chosen = next((c for c in candidates if c["stock"] >= needed), None)
            if chosen is None:
                total_available = sum(c["stock"] for c in candidates)
                result.unresolved.append(UnresolvedPosition(assortment_id=assortment_id, needed=needed, available=total_available))
                continue
            moysklad.set_position_slot(demand["id"], position["id"], store_id=store_id, slot_id=chosen["slotId"])
            chosen["stock"] -= needed
            result.assigned.append({"position_id": position["id"], "assortment_id": assortment_id, "slot_id": chosen["slotId"], "quantity": needed})
        results.append(result)
    return results
