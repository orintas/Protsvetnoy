from sync_service.shift_backfill import apply_day, plan_store
from sync_service.store_mapping import StoreMapping

STORE = StoreMapping(
    novicloud_store_id=88,
    name="Wola Park",
    moysklad_store_id="store-1",
    retail_store_id="retailstore-1",
    organization_id="org-1",
    owner_id="owner-1",
    department_id="dept-1",
    currency_id="cur-1",
)


def _ref(shift_id):
    return {"meta": {"href": f"https://x/entity/retailshift/{shift_id}"}}


def _doc(name, moment, shift_id):
    return {"name": name, "moment": moment, "retailShift": _ref(shift_id), "meta": {"href": f"https://x/entity/retaildemand/{name}"}}


class FakeMoySklad:
    def __init__(self, shifts, demands):
        self.rows = {"retailshift": shifts, "retaildemand": demands, "retailsalesreturn": []}
        self.created, self.moved, self.closed = [], [], []

    def retail_entities_since(self, entity, retail_store_id, since_day):
        return self.rows[entity]

    def create_retail_shift(self, **kwargs):
        self.created.append(kwargs)
        return {"id": "new", "name": "00700"}

    def move_to_retail_shift(self, entity, documents, shift_id):
        self.moved.append((entity, [d["name"] for d in documents], shift_id))

    def close_retail_shift(self, shift_id, close_date, *, name=None):
        self.closed.append((shift_id, close_date))


def test_plan_splits_a_long_running_shift_into_one_per_day():
    # Real shape from Wola Park: shift 00637 opened 2026-08-24 and never closed, holding every later receipt.
    moysklad = FakeMoySklad(
        shifts=[{"id": "s637", "name": "00637", "moment": "2026-08-24 11:05:00.000"}],
        demands=[
            _doc("P/1", "2026-08-24 12:00:00.000", "s637"),
            _doc("P/2", "2026-08-25 12:39:00.000", "s637"),
            _doc("P/3", "2026-08-25 18:00:00.000", "s637"),
            _doc("P/4", "2026-10-14 10:00:00.000", "s637"),
        ],
    )

    plans = plan_store(moysklad, STORE, since_day="2026-08-24", today="2026-10-14")

    by_day = {p.day: p for p in plans}
    assert by_day["2026-08-24"].shift_id == "s637"
    assert by_day["2026-08-24"].moves == {}
    assert by_day["2026-08-24"].close_date == "2026-08-24 23:59:59.000"
    assert by_day["2026-08-25"].shift_id is None
    assert by_day["2026-08-25"].open_moment == "2026-08-25 12:39:00.000"
    assert [d["name"] for d in by_day["2026-08-25"].moves["retaildemand"]] == ["P/2", "P/3"]
    assert by_day["2026-10-14"].close_date is None  # today's shift stays open

    apply_day(moysklad, STORE, by_day["2026-08-25"])
    assert moysklad.created[0]["moment"] == "2026-08-25 12:39:00.000"
    assert moysklad.moved == [("retaildemand", ["P/2", "P/3"], "new")]
    assert moysklad.closed == [("new", "2026-08-25 23:59:59.000")]


def test_plan_keeps_a_valid_close_made_by_the_pos():
    moysklad = FakeMoySklad(
        shifts=[{"id": "s636", "name": "00636", "moment": "2026-08-23 11:08:00.000", "closeDate": "2026-08-24 01:44:00.000"}],
        demands=[_doc("P/1", "2026-08-23 15:00:00.000", "s636")],
    )

    assert plan_store(moysklad, STORE, since_day="2026-08-23", today="2026-10-14") == []
