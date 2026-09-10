from sync_service.store_mapping import find_by_novicloud_store_id, load_store_mappings


def test_wroclavia_mapping():
    mapping = find_by_novicloud_store_id(
        100,
        load_store_mappings("data/store-mappings.json"),
    )
    assert mapping.name == "Wroclavia"
    assert mapping.moysklad_store_id == "a337abc2-968f-11ef-0a80-08f60006abcb"
