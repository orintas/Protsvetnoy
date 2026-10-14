"""One-off repair: split the Poland stores' receipts back into one shift per day.

From 2026-08-24 the nightly shift close stopped happening for the Novicloud
stores, and the sales sync kept attaching every new receipt to whichever
shift was still open — so weeks of receipts ended up in one or two shifts
per store. This rebuilds the per-day shifts: for each day it reuses the
shift opened that day (or creates one, opened at the day's first receipt),
moves that day's receipts and returns into it, and closes it at the end
of the day. Dry run by default — it only prints the plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .moysklad import MoySkladClient
from .shift_closer import _close_with_renamed_fallback
from .store_mapping import StoreMapping

ENTITIES = ("retaildemand", "retailsalesreturn")


@dataclass
class DayPlan:
    day: str
    shift_id: str | None  # None — a new shift has to be created
    shift_name: str | None
    open_moment: str
    close_date: str | None  # None — leave closeDate as it is
    old_close_date: str | None
    moves: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _id(ref: Any) -> str:
    href = ((ref or {}).get("meta") or {}).get("href") or ""
    return href.rsplit("/", 1)[-1]


def plan_store(moysklad: MoySkladClient, store: StoreMapping, *, since_day: str, today: str) -> list[DayPlan]:
    shifts_by_day: dict[str, dict[str, Any]] = {}
    for shift in moysklad.retail_entities_since("retailshift", store.retail_store_id, since_day):
        shifts_by_day.setdefault(str(shift["moment"])[:10], shift)  # earliest shift of the day wins

    docs_by_day: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for entity in ENTITIES:
        for doc in moysklad.retail_entities_since(entity, store.retail_store_id, since_day):
            docs_by_day.setdefault(str(doc["moment"])[:10], {e: [] for e in ENTITIES})[entity].append(doc)

    plans: list[DayPlan] = []
    for day in sorted(docs_by_day):
        if day > today:
            continue
        docs = docs_by_day[day]
        shift = shifts_by_day.get(day)
        first_moment = min(str(doc["moment"]) for entity_docs in docs.values() for doc in entity_docs)
        last_moment = max(str(doc["moment"]) for entity_docs in docs.values() for doc in entity_docs)
        shift_id = str(shift["id"]) if shift else None
        old_close = shift.get("closeDate") if shift else None

        close_date = None
        if day < today:
            # Keep a close the POS/Make made itself (e.g. 01:44 the next night) as long as it covers the day's receipts.
            if not old_close or str(old_close) < last_moment or str(old_close)[:10] < day:
                close_date = f"{day} 23:59:59.000"

        moves = {entity: [doc for doc in entity_docs if _id(doc.get("retailShift")) != shift_id] for entity, entity_docs in docs.items()}
        if shift_id is None or close_date or any(moves.values()):
            plans.append(DayPlan(
                day=day,
                shift_id=shift_id,
                shift_name=shift.get("name") if shift else None,
                open_moment=str(shift["moment"]) if shift else first_moment,
                close_date=close_date,
                old_close_date=old_close,
                moves={entity: entity_docs for entity, entity_docs in moves.items() if entity_docs},
            ))
    return plans


def apply_day(moysklad: MoySkladClient, store: StoreMapping, plan: DayPlan) -> str:
    """Returns the name of the shift the day ended up in."""
    shift_id, shift_name = plan.shift_id, plan.shift_name
    if shift_id is None:
        created = moysklad.create_retail_shift(
            organization_id=store.organization_id,
            store_id=store.moysklad_store_id,
            retail_store_id=store.retail_store_id,
            department_id=store.department_id,
            owner_id=store.owner_id,
            moment=plan.open_moment,
        )
        shift_id, shift_name = str(created["id"]), created.get("name")
    for entity, docs in plan.moves.items():
        moysklad.move_to_retail_shift(entity, docs, shift_id)
    if plan.close_date:
        try:
            moysklad.close_retail_shift(shift_id, plan.close_date)
        except Exception as error:
            if "уникальности" not in str(error).lower():
                raise
            shift_name = _close_with_renamed_fallback(moysklad, shift_id, plan.close_date, shift_name)
            if shift_name is None:
                raise
    return str(shift_name)


def describe(plan: DayPlan) -> str:
    target = f"смена №{plan.shift_name}" if plan.shift_id else f"НОВАЯ смена (открытие {plan.open_moment[:16]})"
    moved = ", ".join(f"{len(docs)} {'чеков' if entity == 'retaildemand' else 'возвратов'}" for entity, docs in plan.moves.items()) or "ничего не переносится"
    was = f" (было: {str(plan.old_close_date)[:16] if plan.old_close_date else 'открыта'})" if plan.shift_id else ""
    close = f"; закрыть {plan.close_date[:16]}{was}" if plan.close_date else ""
    return f"{plan.day}: {target} ← {moved}{close}"
