from sync_service.yandex_market_sync import YandexMarketSyncLog


def test_yandex_market_sync_log_persists_recent_entries(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym_sync.sqlite3"))
    log.add("order", "dry-run", "Заказ найден", "12345", {"orderId": 12345})
    log.add("order", "dry-run", "Повтор", "12345", {"orderId": 12345})
    assert log.recent(1)[0]["external_id"] == "12345"
    assert len(log.recent()) == 1


def test_yandex_market_sync_log_allows_multiple_kinds(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym_sync.sqlite3"))
    log.add("order", "dry-run", "Заказ найден", "1", {})
    log.add("return", "dry-run", "Возврат найден", "1", {})
    assert len(log.recent()) == 2


def test_search_finds_an_order_outside_the_recent_window(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym_sync.sqlite3"))
    log.add("order_created", "success", "Старый заказ создан", "61703852544")
    for i in range(150):
        log.add("order_created", "success", f"Заказ {i} создан", str(1000 + i))

    assert "61703852544" not in {e["external_id"] for e in log.recent()}

    found = log.search("61703852544")
    assert len(found) == 1
    assert found[0]["external_id"] == "61703852544"


def test_has_success_checks_kind_and_status(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym_sync.sqlite3"))
    assert log.has_success("label_sent", "1") is False
    log.add("label_sent", "error", "failed", "1")
    assert log.has_success("label_sent", "1") is False
    log.add("label_sent", "success", "sent", "2")
    assert log.has_success("label_sent", "2") is True
    assert log.has_success("order_created", "2") is False


def test_count_matching_counts_by_external_id_prefix(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym_sync.sqlite3"))
    assert log.count_matching("label_retry_error", "999:") == 0
    log.add("label_retry_error", "error", "attempt 1", "999:1")
    log.add("label_retry_error", "error", "attempt 2", "999:2")
    log.add("label_retry_error", "error", "unrelated order", "1000:1")
    assert log.count_matching("label_retry_error", "999:") == 2
