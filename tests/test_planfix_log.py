from sync_service.planfix_log import PlanFixSyncLog


def test_add_and_recent_round_trip(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    log.add("webhook", "success", "Получено уведомление PlanFix", None, {"event": "contactUpdate"})

    entries = log.recent()

    assert len(entries) == 1
    assert entries[0]["kind"] == "webhook"
    assert entries[0]["status"] == "success"
    assert entries[0]["message"] == "Получено уведомление PlanFix"


def test_search_matches_message_text(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    log.add("webhook", "success", "Контакт 123 обновлён")
    log.add("webhook", "success", "Контакт 456 создан")

    found = log.search("123")

    assert len(found) == 1
    assert "123" in found[0]["message"]


def test_search_with_no_match_returns_empty(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    log.add("webhook", "success", "Контакт 123 обновлён")

    assert log.search("nonexistent") == []
