from sync_service.sync_log import SyncLog, _items


def test_items_accepts_novicloud_sales_payload():
    assert _items({"dane": [{"id": 7}]}) == [{"id": 7}]


def test_sync_log_persists_recent_entries(tmp_path):
    log = SyncLog(str(tmp_path / "sync.sqlite3"))
    log.add("sale", "dry-run", "Продажа найдена", "S-1", {"id": "S-1"})
    log.add("sale", "dry-run", "Повтор", "S-1", {"id": "S-1"})
    assert log.recent(1)[0]["external_id"] == "S-1"
    assert len(log.recent()) == 1
