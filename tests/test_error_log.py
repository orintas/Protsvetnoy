from sync_service.error_log import ErrorLog


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
