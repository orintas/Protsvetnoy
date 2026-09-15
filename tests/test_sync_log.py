from sync_service.sync_log import SyncLog


def test_sync_log_persists_recent_entries(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    log.add("sale", "success", "Продажа создана", "S-1", {"id": "S-1"})
    log.add("sale", "success", "Повтор", "S-1", {"id": "S-1"})
    assert log.recent(1)[0]["external_id"] == "S-1"
    assert len(log.recent()) == 1


def test_sync_log_allows_multiple_kinds(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    log.add("sale", "success", "Продажа создана", "S-1", {})
    log.add("return", "success", "Возврат создан", "S-1", {})
    assert len(log.recent()) == 2


def test_search_finds_a_document_outside_the_recent_window(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    log.add("sale", "success", "Старая продажа", "P/OLD-0001")
    for i in range(150):
        log.add("sale", "success", f"Продажа {i}", f"P/{i:04d}")

    assert "P/OLD-0001" not in {e["external_id"] for e in log.recent()}

    found = log.search("OLD-0001")
    assert len(found) == 1
    assert found[0]["external_id"] == "P/OLD-0001"


def test_search_matches_message_text_too(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    log.add("sale", "success", "Westfield Mokotow: чек P/0123/09/26/K3 создан", "P/0123/09/26/K3")
    log.add("sale", "success", "Manufaktura: чек P/9999 создан", "P/9999")

    found = log.search("Westfield")
    assert [e["external_id"] for e in found] == ["P/0123/09/26/K3"]


def test_search_with_no_match_returns_empty(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    log.add("sale", "success", "Продажа создана", "S-1")
    assert log.search("nonexistent") == []
