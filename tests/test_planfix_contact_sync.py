import pytest

from sync_service.moysklad import PLANFIX_DEPARTMENT_GROUPS
from sync_service.planfix_contact_sync import NotACompany, UnknownDepartment, sync_contact


class FakePlanFixClient:
    def __init__(self, *, contact: dict, moysklad_id_field_id: str | None = "99"):
        self._contact = contact
        self._moysklad_id_field_id = moysklad_id_field_id

    def contact_custom_field_id(self, field_name: str) -> str | None:
        assert field_name == "MoySkladID"
        return self._moysklad_id_field_id

    def get_contact(self, contact_id: str, *, extra_field_ids=()) -> dict:
        return self._contact


class FakeMoySkladClient:
    def __init__(self, *, by_email: dict | None = None, reports: dict | None = None):
        self._by_email = by_email
        self._reports = reports or {}
        self.created: list[dict] = []
        self.updated: list[dict] = []

    def counterparty_by_email(self, email: str) -> dict | None:
        return self._by_email

    def counterparty_report(self, counterparty_id: str) -> dict | None:
        return self._reports.get(counterparty_id)

    def create_counterparty(self, *, name, email, phone, group_id) -> dict:
        result = {"id": "new-id", "meta": {"uuidHref": "https://example/new-id"}}
        self.created.append({"name": name, "email": email, "phone": phone, "group_id": group_id})
        return result

    def update_counterparty(self, counterparty_id, *, name, email, phone, group_id) -> dict:
        self.updated.append({"id": counterparty_id, "name": name, "email": email, "phone": phone, "group_id": group_id})
        return {"id": counterparty_id}


def _contact(**overrides) -> dict:
    base = {
        "isCompany": True,
        "name": "ООО Ромашка",
        "email": "romashka@example.com",
        "phones": [{"number": "+70000000000"}],
        "customFieldData": [],
    }
    base.update(overrides)
    return base


def test_raises_not_a_company_for_a_person_contact():
    planfix = FakePlanFixClient(contact=_contact(isCompany=False))
    moysklad = FakeMoySkladClient()

    with pytest.raises(NotACompany):
        sync_contact(planfix, moysklad, contact_id="1", department_raw="Varvikas")


def test_raises_unknown_department_when_no_marker_matches():
    planfix = FakePlanFixClient(contact=_contact())
    moysklad = FakeMoySkladClient()

    with pytest.raises(UnknownDepartment):
        sync_contact(planfix, moysklad, contact_id="1", department_raw="Что-то другое")


def test_creates_a_new_counterparty_when_nothing_is_known(tmp_path):
    planfix = FakePlanFixClient(contact=_contact())
    moysklad = FakeMoySkladClient(by_email=None)

    result = sync_contact(planfix, moysklad, contact_id="1", department_raw="Varvikas")

    assert result == {
        "MoySkladID": "new-id",
        "MoySkladUrl": "https://example/new-id",
        "CompanyName": "ООО Ромашка",
        "moysklad_url": "https://example/new-id",
        "moysklad_label": "ООО Ромашка",
    }
    assert moysklad.created == [{"name": "ООО Ромашка", "email": "romashka@example.com", "phone": "+70000000000", "group_id": PLANFIX_DEPARTMENT_GROUPS["Varvikas"]}]


def test_updates_and_returns_stats_when_moyskladid_already_stored():
    contact = _contact(customFieldData=[{"field": {"id": 99}, "value": "existing-id"}])
    planfix = FakePlanFixClient(contact=contact)
    moysklad = FakeMoySkladClient(reports={
        "existing-id": {
            "meta": {"uuidHref": "https://example/existing-id"},
            "firstDemandDate": "2026-01-01",
            "lastDemandDate": "2026-02-01",
            "demandsCount": 3,
            "demandsSum": 150000,
        }
    })

    result = sync_contact(planfix, moysklad, contact_id="1", department_raw="Цветной")

    assert result == {
        "MoySkladID": "existing-id",
        "MoySkladUrl": "https://example/existing-id",
        "FirstDemandDate": "2026-01-01",
        "LastDemandDate": "2026-02-01",
        "DemandsCount": 3,
        "DemandsSum": 1500.0,
        "CompanyName": "ООО Ромашка",
        "moysklad_url": "https://example/existing-id",
        "moysklad_label": "ООО Ромашка",
    }
    assert moysklad.updated == [{"id": "existing-id", "name": "ООО Ромашка", "email": "romashka@example.com", "phone": "+70000000000", "group_id": PLANFIX_DEPARTMENT_GROUPS["Цветной"]}]


def test_recreates_when_stored_moyskladid_no_longer_exists():
    contact = _contact(customFieldData=[{"field": {"id": 99}, "value": "deleted-id"}])
    planfix = FakePlanFixClient(contact=contact)
    moysklad = FakeMoySkladClient(reports={})  # counterparty_report returns None for "deleted-id"

    result = sync_contact(planfix, moysklad, contact_id="1", department_raw="Varvikas")

    assert result == {
        "MoySkladID": "new-id",
        "MoySkladUrl": "https://example/new-id",
        "CompanyName": "ООО Ромашка",
        "moysklad_url": "https://example/new-id",
        "moysklad_label": "ООО Ромашка",
    }
    assert len(moysklad.created) == 1


def test_falls_back_to_email_search_when_no_moyskladid_stored():
    planfix = FakePlanFixClient(contact=_contact())
    moysklad = FakeMoySkladClient(
        by_email={"id": "found-by-email"},
        reports={"found-by-email": {"meta": {"uuidHref": "https://example/found-by-email"}}},
    )

    result = sync_contact(planfix, moysklad, contact_id="1", department_raw="Varvikas")

    assert result["MoySkladID"] == "found-by-email"
    assert moysklad.updated[0]["id"] == "found-by-email"
    assert moysklad.created == []


def test_name_falls_back_to_email_and_strips_quotes():
    contact = _contact(name='ООО "Ромашка"', email="romashka@example.com")
    planfix = FakePlanFixClient(contact=contact)
    moysklad = FakeMoySkladClient()

    sync_contact(planfix, moysklad, contact_id="1", department_raw="Varvikas")

    assert moysklad.created[0]["name"] == "ООО 'Ромашка'"
