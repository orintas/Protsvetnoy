import httpx

import sync_service.error_log as error_log_module
from sync_service.error_log import ErrorLog, OutageTracker, is_transient_network_error


def test_error_log_tracks_unread_count(tmp_path):
    log = ErrorLog(str(tmp_path / "errors.sqlite3"))
    assert log.unread_count() == 0
    log.add("moysklad", "GET /entity/product failed with HTTP 500", "traceback...")
    log.add("novicloud", "GET /towary failed with HTTP 502", "traceback...")
    assert log.unread_count() == 2
    assert len(log.recent()) == 2
    log.mark_all_read()
    assert log.unread_count() == 0
    assert log.recent()[0]["read_at"] is not None


def test_add_defaults_to_error_level_and_accepts_info(tmp_path):
    log = ErrorLog(str(tmp_path / "errors.sqlite3"))
    log.add("moysklad", "GET /entity/product failed with HTTP 500")
    log.add("health_check", "X: снова работает", level="info")

    entries = log.recent()
    assert entries[0]["level"] == "info"
    assert entries[1]["level"] == "error"


def test_migrates_an_existing_table_missing_the_level_column(tmp_path):
    import sqlite3

    path = str(tmp_path / "errors.sqlite3")
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE error_log (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL,
            message TEXT NOT NULL, details TEXT NOT NULL, read_at TEXT)"""
        )

    log = ErrorLog(path)  # migration runs on open
    log.add("moysklad", "GET /entity/product failed with HTTP 500")

    assert log.recent()[0]["level"] == "error"


def test_log_exception_captures_traceback(tmp_path):
    log = ErrorLog(str(tmp_path / "errors.sqlite3"))
    try:
        raise ValueError("boom")
    except ValueError as error:
        log.log_exception("test", error, context="During test")
    entry = log.recent()[0]
    assert entry["message"] == "During test: boom"
    assert "ValueError: boom" in entry["details"]
    assert "Traceback" in entry["details"]
    assert entry["read_at"] is None


def test_is_transient_network_error():
    assert is_transient_network_error(httpx.ConnectError("[Errno 104] Connection reset by peer"))
    assert is_transient_network_error(httpx.ConnectTimeout("_ssl.c:993: The handshake operation timed out"))
    assert is_transient_network_error(RuntimeError('Telegram getUpdates failed with HTTP 502: {"ok":false}'))
    assert not is_transient_network_error(RuntimeError("Telegram getUpdates failed with HTTP 409: Conflict"))
    assert not is_transient_network_error(KeyError("update_id"))


def test_outage_tracker_folds_a_short_outage_into_one_info_row(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(error_log_module.time, "monotonic", lambda: clock["now"])
    log = ErrorLog(str(tmp_path / "errors.sqlite3"))
    tracker = OutageTracker(log, "telegram_qa_worker", threshold_seconds=300)

    for _ in range(5):
        tracker.failed(httpx.ConnectError("Connection reset by peer"), context="Ошибка воркера")
        clock["now"] += 5
    tracker.succeeded()

    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["level"] == "info"
    assert log.unread_count() == 0


def test_outage_tracker_escalates_a_long_outage_once_then_logs_recovery(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(error_log_module.time, "monotonic", lambda: clock["now"])
    log = ErrorLog(str(tmp_path / "errors.sqlite3"))
    tracker = OutageTracker(log, "telegram_qa_worker", threshold_seconds=300)

    for _ in range(100):  # 500 s of failures every 5 s
        tracker.failed(httpx.ConnectError("Connection reset by peer"), context="Ошибка воркера")
        clock["now"] += 5
    tracker.succeeded()

    levels = [e["level"] for e in reversed(log.recent())]
    assert levels == ["info", "error", "info"]
    assert "восстановилась" in log.recent()[0]["message"]
    assert log.unread_count() == 1


def test_outage_tracker_logs_non_network_errors_every_time(tmp_path):
    log = ErrorLog(str(tmp_path / "errors.sqlite3"))
    tracker = OutageTracker(log, "telegram_qa_worker")

    tracker.failed(ValueError("bad payload"), context="Ошибка воркера")
    tracker.failed(ValueError("bad payload"), context="Ошибка воркера")

    assert [e["level"] for e in log.recent()] == ["error", "error"]
