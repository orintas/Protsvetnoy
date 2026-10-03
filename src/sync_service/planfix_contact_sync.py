from __future__ import annotations

from typing import Any

from .moysklad import PLANFIX_DEPARTMENT_GROUPS, MoySkladClient
from .moysklad_links import moysklad_link_fields
from .planfix_client import PlanFixClient

MOYSKLAD_ID_FIELD_NAME = "MoySkladID"


class NotACompany(Exception):
    """The PlanFix contact is a person, not a company — this button is only
    meant for company contacts (mirrors the Make scenario it replaces,
    which responded 400 "Это не компания" for the same case)."""


class UnknownDepartment(Exception):
    """The posted "Отдел" value doesn't contain any of the known department
    markers — rather than guess a MoySklad group, surface this so it gets
    fixed (new department added without updating PLANFIX_DEPARTMENT_GROUPS)."""

    def __init__(self, department_raw: str) -> None:
        super().__init__(f"Не удалось определить группу МойСклад по отделу «{department_raw}»")
        self.department_raw = department_raw


def _group_id_for_department(department_raw: str) -> str:
    for marker, group_id in PLANFIX_DEPARTMENT_GROUPS.items():
        if marker in department_raw:
            return group_id
    raise UnknownDepartment(department_raw)


def _custom_field_value(contact: dict[str, Any], field_id: str | None) -> str:
    """Extracts a custom field's value from a contact's customFieldData.

    PlanFix's own docs describe the value shape as depending on the field's
    type (oneOf several shapes) without a concrete example — this handles a
    plain string and a {"value": ...} wrapper, which covers a simple text
    field either way. Needs confirming against a live MoySkladID field
    before fully trusting it; a wrong guess here just means a contact gets
    treated as new (no stored id found) rather than anything destructive.
    """
    if field_id is None:
        return ""
    for entry in contact.get("customFieldData", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("field", {}).get("id")) != field_id:
            continue
        value = entry.get("value")
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get("value") or value.get("text") or value.get("name") or "")
    return ""


def _contact_name(contact: dict[str, Any]) -> str:
    name = contact.get("name") or contact.get("email") or ""
    return name.replace('"', "'")


def _first_phone(contact: dict[str, Any]) -> str:
    phones = contact.get("phones") or []
    if not phones:
        return ""
    first = phones[0]
    return first.get("number", "") if isinstance(first, dict) else str(first)


def sync_contact(planfix: PlanFixClient, moysklad: MoySkladClient, *, contact_id: str, department_raw: str) -> dict[str, Any]:
    """Transfers a PlanFix company contact into MoySklad as a контрагент and
    returns the JSON the PlanFix button is already configured to parse back
    into its own fields (MoySkladID, MoySkladUrl, and — only when updating
    an existing counterparty — the sales-stats fields). Field names in the
    returned dict are fixed by that existing button configuration; changing
    them breaks the button's response parsing.
    """
    moysklad_id_field = planfix.contact_custom_field_id(MOYSKLAD_ID_FIELD_NAME)
    contact = planfix.get_contact(contact_id, extra_field_ids=[moysklad_id_field] if moysklad_id_field else [])
    if not contact.get("isCompany"):
        raise NotACompany()

    group_id = _group_id_for_department(department_raw)
    name = _contact_name(contact)
    email = contact.get("email") or ""
    phone = _first_phone(contact)

    moysklad_id = _custom_field_value(contact, moysklad_id_field)
    if not moysklad_id:
        existing = moysklad.counterparty_by_email(email)
        if existing:
            moysklad_id = existing["id"]

    if moysklad_id:
        stats = moysklad.counterparty_report(moysklad_id)
        if stats is not None:
            moysklad.update_counterparty(moysklad_id, name=name, email=email, phone=phone, group_id=group_id)
            return {
                "MoySkladID": moysklad_id,
                "MoySkladUrl": stats.get("meta", {}).get("uuidHref", ""),
                "FirstDemandDate": stats.get("firstDemandDate"),
                "LastDemandDate": stats.get("lastDemandDate"),
                "DemandsCount": stats.get("demandsCount"),
                "DemandsSum": (stats.get("demandsSum") or 0) / 100,
                "CompanyName": name,
                **moysklad_link_fields(stats, name),
            }

    created = moysklad.create_counterparty(name=name, email=email, phone=phone, group_id=group_id)
    return {
        "MoySkladID": created["id"],
        "MoySkladUrl": created.get("meta", {}).get("uuidHref", ""),
        "CompanyName": name,
        **moysklad_link_fields(created, name),
    }
