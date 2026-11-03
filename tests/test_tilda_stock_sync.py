import json
import xml.etree.ElementTree as ET
from datetime import datetime
from types import SimpleNamespace

import sync_service.tilda_stock_sync as stock_module
from sync_service.tilda_stock_sync import (
    SITE_STORES,
    import_xml,
    is_site_code,
    offers_xml,
    run_once,
    site_stock_document,
    stock_snapshot,
)
from sync_service.yandex_market_stock_sync import AssortmentCache
from sync_service.yandex_market_sync import YandexMarketSyncLog

NOW = datetime.fromisoformat("2026-11-02T14:10:00+03:00")


class FakeMoySklad:
    def __init__(self, stock=None, products=None):
        self.stock = stock or {}
        self.products = products or []

    def stock_by_store(self, store_id):
        return self.stock.get(store_id, [])

    def products_by_group(self, group_id):
        return self.products

    def close(self):
        pass


def test_european_codes_are_not_site_products():
    assert is_site_code("MG2112") is True
    assert is_site_code("MG2112e") is False
    assert is_site_code("") is False


def test_snapshot_uses_stock_minus_reserve_floored_and_drops_zeroes():
    moysklad = FakeMoySklad(stock={
        SITE_STORES["main"]: [{"code": "RB003", "stock": 3, "reserve": 1}, {"code": "SOLD", "stock": 1, "reserve": 2}],
        SITE_STORES["aviapark"]: [{"code": "RB003", "stock": 1.7, "reserve": 0}, {"stock": 5}],
    })
    assert stock_snapshot(moysklad) == {"RB003": {"main": 2, "aviapark": 1}}


def test_stock_document_lists_only_site_products_in_stock():
    snapshot = {"RB003": {"main": 2, "riviera": 1}, "MG2112e": {"main": 4}}
    document = site_stock_document(snapshot, {"RB003", "MG2142"}, NOW)
    assert document == {"updated": "2026-11-02T14:10:00+03:00", "items": {"RB003": {"main": 2, "riviera": 1}}}


def test_commerceml_files_carry_code_as_both_ids_and_no_prices():
    products = [{"code": "RB003", "name": "Цветочная <Оранжерея> & Ко"}]
    catalog = ET.fromstring(import_xml(products, NOW))
    product = catalog.find("Каталог/Товары/Товар")
    assert catalog.find("Каталог").get("СодержитТолькоИзменения") == "true"
    assert product.findtext("Ид") == "RB003" and product.findtext("Артикул") == "RB003"
    assert product.findtext("Наименование") == "Цветочная <Оранжерея> & Ко"

    offers = ET.fromstring(offers_xml(products + [{"code": "LG042", "name": "x"}], {"RB003": 7}, NOW))
    rows = offers.findall("ПакетПредложений/Предложения/Предложение")
    assert [(r.findtext("Ид"), r.findtext("Количество")) for r in rows] == [("RB003", "7"), ("LG042", "0")]
    assert offers.find(".//Цены") is None


def _settings(enabled):
    return SimpleNamespace(moysklad_base_url="https://ms", moysklad_token="t", tilda_stock_sync_enabled=enabled,
                           tilda_cml_login="208015", tilda_cml_password="p", tilda_cml_url="https://tilda")


def _patch_moysklad(monkeypatch):
    moysklad = FakeMoySklad(
        stock={SITE_STORES["main"]: [{"code": "RB003", "stock": 2, "reserve": 0}], SITE_STORES["salaris"]: [{"code": "RB003", "stock": 1, "reserve": 0}]},
        products=[{"code": "RB003", "name": "Оранжерея"}, {"code": "LG042", "name": "Парусник"}, {"code": "MG2112e", "name": "EU"}],
    )
    monkeypatch.setattr(stock_module, "MoySkladClient", lambda **kwargs: moysklad)


def test_run_once_writes_stock_json_and_skips_tilda_while_disabled(tmp_path, monkeypatch):
    _patch_moysklad(monkeypatch)
    monkeypatch.setattr(stock_module, "push_to_tilda", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not push")))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    path = tmp_path / "site_stock.json"

    run_once(_settings(False), log, AssortmentCache(str(tmp_path / "state.sqlite3")), now=NOW, stock_json_path=str(path))

    assert json.loads(path.read_text())["items"] == {"RB003": {"main": 2, "salaris": 1}}
    entry = log.recent()[0]
    assert entry["kind"] == "stock_sync" and entry["status"] == "success" and "выключена" in entry["message"]


def test_run_once_pushes_totals_of_all_stores_and_logs_changes(tmp_path, monkeypatch):
    _patch_moysklad(monkeypatch)
    pushed = []
    monkeypatch.setattr(stock_module, "push_to_tilda", lambda settings, products, quantities, now: pushed.append((products, quantities)) or [])
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    state = AssortmentCache(str(tmp_path / "state.sqlite3"))

    run_once(_settings(True), log, state, now=NOW, stock_json_path=str(tmp_path / "s.json"))
    run_once(_settings(True), log, state, now=NOW, stock_json_path=str(tmp_path / "s.json"))

    products, quantities = pushed[0]
    assert [p["code"] for p in products] == ["LG042", "RB003"]  # European MG2112e left out
    assert quantities == {"LG042": 0, "RB003": 3}
    first, second = reversed([e["message"] for e in log.recent()])
    assert "первая синхронизация" in first
    assert "изменений нет" in second
