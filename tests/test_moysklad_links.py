from sync_service.moysklad_links import moysklad_link_fields


def test_moysklad_link_fields_reads_uuid_href():
    entity = {"id": "cp-1", "meta": {"uuidHref": "https://online.moysklad.ru/app/#company/edit?id=cp-1"}}
    assert moysklad_link_fields(entity, "ООО Ромашка") == {
        "moysklad_url": "https://online.moysklad.ru/app/#company/edit?id=cp-1",
        "moysklad_label": "ООО Ромашка",
    }


def test_moysklad_link_fields_handles_missing_meta():
    assert moysklad_link_fields(None, "ООО Ромашка") == {"moysklad_url": "", "moysklad_label": "ООО Ромашка"}
    assert moysklad_link_fields({}, "ООО Ромашка") == {"moysklad_url": "", "moysklad_label": "ООО Ромашка"}
