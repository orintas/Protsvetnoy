from datetime import datetime, timezone

from sync_service.shift_closer import TARGET_ORGANIZATIONS, ShiftCloseLog, run_once


class FakeMoySkladClient:
    def __init__(self, shifts_by_org: dict[str, list[dict]]):
        self.shifts_by_org = shifts_by_org
        self.closed: list[tuple[str, str]] = []
        self.fail_ids: set[str] = set()
        self.already_closed_ids: set[str] = set()

    def open_retail_shifts(self, organization_id, since):
        return self.shifts_by_org.get(organization_id, [])

    def retail_shift_close_date(self, shift_id):
        return "2026-09-13 21:00:00.000" if shift_id in self.already_closed_ids else None

    def close_retail_shift(self, shift_id, close_date):
        if shift_id in self.fail_ids:
            raise RuntimeError("boom")
        self.closed.append((shift_id, close_date))


def _shift(shift_id: str, name: str) -> dict:
    return {"id": shift_id, "name": name, "moment": "2026-09-09 10:00:00.000", "retailStore": {"meta": {"href": "https://x/retailstore/1"}}}


def test_dry_run_does_not_close_shifts(tmp_path):
    poland_org = next(iter(TARGET_ORGANIZATIONS))
    client = FakeMoySkladClient({poland_org: [_shift("s1", "001")]})
    log = ShiftCloseLog(str(tmp_path / "dry_run.sqlite3"))
    close_moment = datetime(2026, 9, 13, 23, 50, tzinfo=timezone.utc)
    count = run_once(client, log, dry_run=True, close_moment=close_moment)
    assert count == 1
    assert client.closed == []
    entries = log.recent()
    assert any(e["status"] == "dry-run" and "s1" not in e["message"] and "001" in e["message"] for e in entries)


def test_live_run_closes_shifts_and_continues_after_failure(tmp_path):
    orgs = list(TARGET_ORGANIZATIONS)
    client = FakeMoySkladClient({
        orgs[0]: [_shift("ok-1", "001")],
        orgs[1]: [_shift("fail-1", "002")],
    })
    client.fail_ids = {"fail-1"}
    log = ShiftCloseLog(str(tmp_path / "shift_close.sqlite3"))
    close_moment = datetime(2026, 9, 13, 23, 50, tzinfo=timezone.utc)
    count = run_once(client, log, dry_run=False, close_moment=close_moment)
    assert count == 2
    assert client.closed == [("ok-1", "2026-09-13 23:50:00.000")]
    entries = log.recent()
    assert any(e["status"] == "success" and e["kind"] == "shift" for e in entries)
    assert any(e["status"] == "error" and "fail-1" not in e["message"] and "002" in e["message"] for e in entries)


def test_skips_shift_the_store_pos_already_closed_since_listing(tmp_path):
    poland_org = next(iter(TARGET_ORGANIZATIONS))
    client = FakeMoySkladClient({poland_org: [_shift("s1", "00256")]})
    client.already_closed_ids = {"s1"}
    log = ShiftCloseLog(str(tmp_path / "race.sqlite3"))
    close_moment = datetime(2026, 9, 13, 23, 50, tzinfo=timezone.utc)

    count = run_once(client, log, dry_run=False, close_moment=close_moment)

    assert count == 1
    assert client.closed == []  # never called close_retail_shift on an already-closed shift
    entries = log.recent()
    assert any(e["status"] == "success" and "уже закрыта" in e["message"] for e in entries)
    assert not any(e["status"] == "error" for e in entries)


def test_last_run_date_persists(tmp_path):
    log = ShiftCloseLog(str(tmp_path / "state.sqlite3"))
    assert log.last_run_date() is None
    log.set_last_run_date("2026-09-13")
    assert log.last_run_date() == "2026-09-13"
    log.set_last_run_date("2026-09-14")
    assert log.last_run_date() == "2026-09-14"
