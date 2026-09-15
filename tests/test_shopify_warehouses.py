from sync_service.shopify_warehouses import COUNTRY_ORGANIZATIONS, GENERAL_WAREHOUSES, ShopifyWarehouseConfig, available_warehouses


class FakeMoySklad:
    def __init__(self, by_org):
        self.by_org = by_org

    def warehouses_by_organization(self, org_id):
        return self.by_org.get(org_id, [])


def test_available_warehouses_groups_by_country_and_appends_general():
    moysklad = FakeMoySklad({
        "a623f6a9-dde8-11ed-0a80-01540011a4a0": [{"id": "pl-1", "name": "Wola Park"}],
        "0b3fbc43-e0dc-11ec-0a80-0076000f9c69": [{"id": "ee-1", "name": "Rocca al Mare"}],
    })
    result = available_warehouses(moysklad)
    countries = {w["country"] for w in result}
    assert "Польша" in countries
    assert "Эстония" in countries
    assert "Общие склады" in countries
    assert {w["id"] for w in result if w["country"] == "Общие склады"} == {w["id"] for w in GENERAL_WAREHOUSES}


def test_available_warehouses_covers_all_four_countries():
    assert set(COUNTRY_ORGANIZATIONS.values()) == {"Польша", "Литва", "Латвия", "Эстония"}


def test_config_round_trips_selection(tmp_path):
    config = ShopifyWarehouseConfig(str(tmp_path / "warehouses.json"))
    assert config.load() == []
    saved = config.save(["b-id", "a-id", "a-id"])
    assert saved == ["a-id", "b-id"]
    assert config.load() == ["a-id", "b-id"]
