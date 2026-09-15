import pytest

from sync_service.novicloud_retail_sync import run_once, sync_store_returns, sync_store_sales
from sync_service.store_mapping import StoreMapping
from sync_service.sync_log import SyncLog


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    import sync_service.novicloud_retail_sync as mod
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)

STORE = StoreMapping(
    novicloud_store_id=23,
    name="Westfield Mokotow",
    moysklad_store_id="store-1",
    retail_store_id="retailstore-1",
    organization_id="org-1",
    owner_id="owner-1",
    department_id="dept-1",
    currency_id="cur-1",
)


class FakeMoySklad:
    base_url = "https://api.moysklad.ru/api/remap/1.2"

    def __init__(self, *, existing_names=None, open_shift=None):
        self.existing_names = existing_names or set()
        self.open_shift = open_shift
        self.created_shifts = []
        self.created_demands = []
        self.created_returns = []
        self.closed = False

    def last_document_moment(self, entity, retail_store_id):
        return None

    def document_exists(self, entity, *, name, retail_store_id):
        return name in self.existing_names

    def find_open_retail_shift(self, retail_store_id):
        return self.open_shift

    def create_retail_shift(self, **kwargs):
        self.created_shifts.append(kwargs)
        return {"id": "new-shift"}

    def create_retail_demand(self, **kwargs):
        self.created_demands.append(kwargs)
        return {"id": "demand-1"}

    def create_retail_return(self, **kwargs):
        self.created_returns.append(kwargs)
        return {"id": "return-1"}

    def close(self):
        self.closed = True


class FakeNovicloud:
    def __init__(self, *, docs=None, positions_by_link=None, products_by_link=None):
        self.docs = docs or []
        self.positions_by_link = positions_by_link or {}
        self.products_by_link = products_by_link or {}
        self.closed = False

    def documents(self, *, typ_dok, sklep_id, date_from=None):
        return {"dane": self.docs}

    def get_url(self, url):
        if url in self.positions_by_link:
            return self.positions_by_link[url]
        if url in self.products_by_link:
            return self.products_by_link[url]
        raise KeyError(url)

    def close(self):
        self.closed = True


def _sale_doc(nr_dok="P/1", storno=False):
    return {
        "nr_dok": nr_dok,
        "nr_systemowy": "123",
        "nr_fiskalny": "1",
        "data_wystawienia": "2026-09-10T10:31:45",
        "zaplacono": 59.0,
        "storno": storno,
        "platnosci": [{"forma_platnosci": {"id": 2}, "wplata_waluta": 59.0}],
        "pozycje": {"link": "https://novicloud/pozdok?dokument.id=1"},
    }


def _positions_payload(product_link="https://novicloud/towary/1", quantity=1.0, storno=False):
    return {"dane": [{"towar": {"link": product_link}, "ilosc": quantity, "w_brutto": 59.0, "stawka_vat": 2300, "storno": storno}]}


def _product_payload(uuid="prod-uuid-1", kod="SKU1"):
    return {"dane": {"opis_3": uuid, "kod": kod}}


def test_sync_store_sales_creates_document_with_positions_and_cash_split(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    novicloud = FakeNovicloud(
        docs=[_sale_doc()],
        positions_by_link={"https://novicloud/pozdok?dokument.id=1": _positions_payload()},
        products_by_link={"https://novicloud/towary/1": _product_payload()},
    )
    moysklad = FakeMoySklad(open_shift={"id": "existing-shift"})

    sync_store_sales(moysklad, novicloud, STORE, log)

    assert len(moysklad.created_demands) == 1
    demand = moysklad.created_demands[0]
    assert demand["name"] == "P/1"
    assert demand["retail_shift_id"] == "existing-shift"
    assert demand["cash_sum"] == 0
    assert demand["non_cash_sum"] == 5900
    assert demand["document_number"] == 123  # int, not "123" — MoySklad rejects the field as a string (HTTP 400)
    assert demand["positions"] == [{
        "quantity": 1.0,
        "price": 5900,
        "vat": 23,
        "assortment": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/prod-uuid-1", "type": "product", "mediaType": "application/json"}},
    }]
    assert moysklad.created_shifts == []  # reused the existing open shift
    kinds = [e["kind"] for e in log.recent()]
    assert kinds == ["sale"]


def test_sync_store_sales_omits_document_number_when_not_numeric(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    doc = _sale_doc()
    doc["nr_systemowy"] = "P/0123/09/26"  # real-world Wroclavia case: not a plain integer
    novicloud = FakeNovicloud(
        docs=[doc],
        positions_by_link={"https://novicloud/pozdok?dokument.id=1": _positions_payload()},
        products_by_link={"https://novicloud/towary/1": _product_payload()},
    )
    moysklad = FakeMoySklad(open_shift={"id": "existing-shift"})

    sync_store_sales(moysklad, novicloud, STORE, log)

    assert moysklad.created_demands[0]["document_number"] is None


def test_sync_store_sales_creates_shift_when_none_open(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    novicloud = FakeNovicloud(
        docs=[_sale_doc()],
        positions_by_link={"https://novicloud/pozdok?dokument.id=1": _positions_payload()},
        products_by_link={"https://novicloud/towary/1": _product_payload()},
    )
    moysklad = FakeMoySklad(open_shift=None)

    sync_store_sales(moysklad, novicloud, STORE, log)

    assert len(moysklad.created_shifts) == 1
    assert moysklad.created_demands[0]["retail_shift_id"] == "new-shift"


def test_sync_store_sales_skips_already_existing_document(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    novicloud = FakeNovicloud(docs=[_sale_doc()])
    moysklad = FakeMoySklad(existing_names={"P/1"})

    sync_store_sales(moysklad, novicloud, STORE, log)

    assert moysklad.created_demands == []
    assert log.recent() == []


def test_sync_store_sales_skips_canceled_document(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    novicloud = FakeNovicloud(docs=[_sale_doc(storno=True)])
    moysklad = FakeMoySklad()

    sync_store_sales(moysklad, novicloud, STORE, log)

    assert moysklad.created_demands == []


def test_sync_store_sales_logs_error_for_unmapped_product_but_keeps_other_positions(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    doc = _sale_doc()
    novicloud = FakeNovicloud(
        docs=[doc],
        positions_by_link={"https://novicloud/pozdok?dokument.id=1": {"dane": [
            {"towar": {"link": "https://novicloud/towary/1"}, "ilosc": 1.0, "w_brutto": 59.0, "stawka_vat": 2300, "storno": False},
            {"towar": {"link": "https://novicloud/towary/2"}, "ilosc": 1.0, "w_brutto": 10.0, "stawka_vat": 2300, "storno": False},
        ]}},
        products_by_link={
            "https://novicloud/towary/1": _product_payload(),
            "https://novicloud/towary/2": {"dane": {"opis_3": None, "kod": "UNMAPPED"}},
        },
    )
    moysklad = FakeMoySklad(open_shift={"id": "shift-1"})

    sync_store_sales(moysklad, novicloud, STORE, log)

    assert len(moysklad.created_demands[0]["positions"]) == 1
    error_entries = [e for e in log.recent() if e["kind"] == "sale_error"]
    assert len(error_entries) == 1
    assert "UNMAPPED" in error_entries[0]["message"]


def test_sync_store_sales_skips_document_when_no_positions_resolve(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    novicloud = FakeNovicloud(
        docs=[_sale_doc()],
        positions_by_link={"https://novicloud/pozdok?dokument.id=1": _positions_payload()},
        products_by_link={"https://novicloud/towary/1": {"dane": {"opis_3": None, "kod": "UNMAPPED"}}},
    )
    moysklad = FakeMoySklad()

    sync_store_sales(moysklad, novicloud, STORE, log)

    assert moysklad.created_demands == []
    assert moysklad.created_shifts == []  # never even needed a shift


def test_sync_store_returns_negates_payment_split(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    doc = _sale_doc(nr_dok="Z/1")
    novicloud = FakeNovicloud(
        docs=[doc],
        positions_by_link={"https://novicloud/pozdok?dokument.id=1": _positions_payload()},
        products_by_link={"https://novicloud/towary/1": _product_payload()},
    )
    moysklad = FakeMoySklad(open_shift={"id": "shift-1"})

    sync_store_returns(moysklad, novicloud, STORE, log)

    assert len(moysklad.created_returns) == 1
    ret = moysklad.created_returns[0]
    assert ret["name"] == "Z/1"
    assert ret["cash_sum"] == 0
    assert ret["non_cash_sum"] == -5900
    kinds = [e["kind"] for e in log.recent()]
    assert kinds == ["return"]


def test_run_once_logs_a_heartbeat_summary_even_when_nothing_new(tmp_path, monkeypatch):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    moysklad = FakeMoySklad()
    novicloud = FakeNovicloud()

    import sync_service.novicloud_retail_sync as mod
    monkeypatch.setattr(mod, "MoySkladClient", lambda **kwargs: moysklad)
    monkeypatch.setattr(mod, "NovicloudClient", lambda **kwargs: novicloud)

    class FakeSettings:
        moysklad_base_url = "x"
        moysklad_token = "x"
        novicloud_base_url = "x"
        novicloud_api_version = "v2"
        novicloud_account = "x"
        novicloud_password = "x"

    run_once(FakeSettings(), log)

    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["kind"] == "run"
    assert entries[0]["message"] == "Проверка завершена: новых чеков 0, возвратов 0"


def test_run_once_continues_other_stores_after_one_fails(tmp_path, monkeypatch):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))

    class FailingMoySklad(FakeMoySklad):
        def last_document_moment(self, entity, retail_store_id):
            raise RuntimeError("boom")

    moysklad = FailingMoySklad()
    novicloud = FakeNovicloud()

    import sync_service.novicloud_retail_sync as mod
    monkeypatch.setattr(mod, "MoySkladClient", lambda **kwargs: moysklad)
    monkeypatch.setattr(mod, "NovicloudClient", lambda **kwargs: novicloud)

    class FakeSettings:
        moysklad_base_url = "x"
        moysklad_token = "x"
        novicloud_base_url = "x"
        novicloud_api_version = "v2"
        novicloud_account = "x"
        novicloud_password = "x"

    run_once(FakeSettings(), log)

    error_kinds = {e["kind"] for e in log.recent(limit=1000)}
    assert "sale_error" in error_kinds
    assert "return_error" in error_kinds
    assert moysklad.closed and novicloud.closed
