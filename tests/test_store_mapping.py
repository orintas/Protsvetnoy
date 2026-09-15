from sync_service.store_mapping import find_by_novicloud_store_id, load_store_mappings


def test_wroclavia_mapping():
    mapping = find_by_novicloud_store_id(
        100,
        load_store_mappings("config/store-mappings.json"),
    )
    assert mapping.name == "Wroclavia"
    assert mapping.moysklad_store_id == "a337abc2-968f-11ef-0a80-08f60006abcb"
    assert mapping.retail_store_id == "335d7e9a-9690-11ef-0a80-19da00072b49"
    assert mapping.organization_id == "a623f6a9-dde8-11ed-0a80-01540011a4a0"


def test_all_poland_stores_share_org_department_and_currency():
    mappings = load_store_mappings("config/store-mappings.json")
    assert len(mappings) == 3
    assert {m.organization_id for m in mappings} == {"a623f6a9-dde8-11ed-0a80-01540011a4a0"}
    assert {m.department_id for m in mappings} == {"62a11082-1b25-11ea-0a80-030300038a2c"}
    assert {m.currency_id for m in mappings} == {"cae74fea-26ec-11ee-0a80-02b4000b49e4"}
