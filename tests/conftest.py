import pytest

import sync_service.change_log as change_log_module


@pytest.fixture(autouse=True)
def _isolated_change_log(tmp_path, monkeypatch):
    """change_log.record() is called directly from low-level API client
    methods (no log object threaded through every call site), backed by a
    module-level singleton. Without this, every test that exercises a real
    client write method would write to the real data/change_log.sqlite3 and
    leak state between tests via that shared singleton."""
    monkeypatch.setattr(change_log_module, "_instance", change_log_module.ChangeLog(str(tmp_path / "change_log.sqlite3")))
