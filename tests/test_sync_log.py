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
