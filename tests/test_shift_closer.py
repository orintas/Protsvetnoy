from datetime import datetime, timedelta, timezone

from sync_service.shift_closer import CLOSE_HOUR, CLOSE_MINUTE, TARGET_ORGANIZATIONS, ShiftCloseLog, _pending_close_target, run_once


class FakeMoySkladClient:
    def __init__(self, shifts_by_org: dict[str, list[dict]]):
        self.shifts_by_org = shifts_by_org
        self.closed: list[tuple[str, str]] = []
        self.closed_names: list[str] = []  # name each renamed-fallback close actually succeeded under
        self.fail_ids: set[str] = set()
        self.fail_orgs: set[str] = set()
        self.already_closed_ids: set[str] = set()
        # Shifts whose plain close attempt hits MoySklad's name-uniqueness
        # conflict (confirmed live: two same-named shifts open at once in
        # the same store).
        self.fail_with_uniqueness_ids: set[str] = set()
        # Of those, ones where even every renamed retry is rejected too.
        self.reject_all_renames_ids: set[str] = set()
        # Shifts that only become "closed by the POS" once our own close
        # attempt fails — simulates the race losing right at the PUT itself.
        self.close_during_our_attempt_ids: set[str] = set()

    def open_retail_shifts(self, organization_id, since):
        if organization_id in self.fail_orgs:
            raise RuntimeError("boom (503)")
        return self.shifts_by_org.get(organization_id, [])

    def retail_shift_close_date(self, shift_id):
        return "2026-09-13 21:00:00.000" if shift_id in self.already_closed_ids else None

    def close_retail_shift(self, shift_id, close_date, *, name=None):
        if shift_id in self.close_during_our_attempt_ids:
            self.already_closed_ids.add(shift_id)
            raise RuntimeError("boom (412, name uniqueness)")
        if name is not None:
            if shift_id in self.reject_all_renames_ids:
                raise RuntimeError("HTTP 412: Ошибка сохранения объекта: нарушено ограничение уникальности параметра 'name'")
            self.closed.append((shift_id, close_date))
            self.closed_names.append(name)
            return
        if shift_id in self.fail_with_uniqueness_ids:
            raise RuntimeError("HTTP 412: Ошибка сохранения объекта: нарушено ограничение уникальности параметра 'name'")
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


def test_skips_shift_the_store_pos_closes_during_our_own_close_attempt(tmp_path):
    poland_org = next(iter(TARGET_ORGANIZATIONS))
    client = FakeMoySkladClient({poland_org: [_shift("s1", "00257")]})
    client.close_during_our_attempt_ids = {"s1"}
    log = ShiftCloseLog(str(tmp_path / "race2.sqlite3"))
    close_moment = datetime(2026, 9, 13, 23, 50, tzinfo=timezone.utc)

    count = run_once(client, log, dry_run=False, close_moment=close_moment)

    assert count == 1
    assert client.closed == []  # our own close_retail_shift call did fail
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


def test_one_country_failing_to_list_shifts_does_not_abort_the_others(tmp_path):
    orgs = list(TARGET_ORGANIZATIONS)
    client = FakeMoySkladClient({orgs[1]: [_shift("ok-1", "001")]})
    client.fail_orgs = {orgs[0]}  # Poland's own listing call 503s
    log = ShiftCloseLog(str(tmp_path / "shift_close.sqlite3"))
    close_moment = datetime(2026, 9, 13, 23, 50, tzinfo=timezone.utc)

    count = run_once(client, log, dry_run=False, close_moment=close_moment)

    assert count == 1  # Lithuania's shift was still found and closed
    assert client.closed == [("ok-1", "2026-09-13 23:50:00.000")]
    entries = log.recent()
    assert any(e["kind"] == "run_error" and e["status"] == "error" for e in entries)
    assert any(e["kind"] == "run" and e["status"] == "success" for e in entries)  # still reaches the summary line


def test_pending_close_target_returns_none_before_close_hour(tmp_path):
    now = datetime(2026, 9, 24, CLOSE_HOUR - 1, 0, tzinfo=timezone.utc)
    assert _pending_close_target(now, None) is None


def test_pending_close_target_fires_at_close_time_when_not_run_today(tmp_path):
    now = datetime(2026, 9, 24, CLOSE_HOUR, CLOSE_MINUTE, tzinfo=timezone.utc)
    target = _pending_close_target(now, "2026-09-23")
    assert target == now


def test_pending_close_target_returns_none_when_already_run_today(tmp_path):
    now = datetime(2026, 9, 24, CLOSE_HOUR, CLOSE_MINUTE, tzinfo=timezone.utc)
    assert _pending_close_target(now, "2026-09-24") is None


def test_pending_close_target_catches_up_a_fully_missed_day_at_any_hour(tmp_path):
    # last successful run was two days before "now" — yesterday's run never
    # completed (e.g. MoySklad 503s right up until the retry window closed
    # at midnight) — must retry yesterday's own close time, not wait for
    # tonight's normally-scheduled run.
    now = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)  # early morning, well before tonight's close hour
    target = _pending_close_target(now, "2026-09-22")
    assert target == datetime(2026, 9, 23, CLOSE_HOUR, CLOSE_MINUTE, tzinfo=timezone.utc)


def test_pending_close_target_does_not_catch_up_when_only_one_day_behind(tmp_path):
    # last_run_date is yesterday — completely normal (just waiting for
    # tonight's close hour), not a missed day.
    now = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)
    assert _pending_close_target(now, "2026-09-23") is None


def test_uniqueness_conflict_is_retried_under_a_suffixed_name(tmp_path):
    poland_org = next(iter(TARGET_ORGANIZATIONS))
    client = FakeMoySkladClient({poland_org: [_shift("s1", "00265")]})
    client.fail_with_uniqueness_ids = {"s1"}
    log = ShiftCloseLog(str(tmp_path / "rename.sqlite3"))
    close_moment = datetime(2026, 9, 23, 23, 50, tzinfo=timezone.utc)

    count = run_once(client, log, dry_run=False, close_moment=close_moment)

    assert count == 1
    assert client.closed == [("s1", "2026-09-23 23:50:00.000")]
    assert client.closed_names == ["00265-1"]  # first suffix already worked
    entries = log.recent()
    assert any(e["status"] == "success" and "00265-1" in e["message"] for e in entries)
    assert not any(e["status"] == "error" for e in entries)


def test_uniqueness_conflict_tries_further_suffixes_when_first_is_also_taken(tmp_path):
    poland_org = next(iter(TARGET_ORGANIZATIONS))
    client = FakeMoySkladClient({poland_org: [_shift("s1", "00265")]})
    client.fail_with_uniqueness_ids = {"s1"}
    # simulate "-1" also being taken by rejecting the very first renamed
    # attempt only, via a thin wrapper
    real_close = client.close_retail_shift
    attempts = []

    def close_with_first_suffix_taken(shift_id, close_date, *, name=None):
        if name == "00265-1":
            attempts.append(name)
            raise RuntimeError("HTTP 412: нарушено ограничение уникальности параметра 'name'")
        return real_close(shift_id, close_date, name=name)

    client.close_retail_shift = close_with_first_suffix_taken
    log = ShiftCloseLog(str(tmp_path / "rename2.sqlite3"))
    close_moment = datetime(2026, 9, 23, 23, 50, tzinfo=timezone.utc)

    count = run_once(client, log, dry_run=False, close_moment=close_moment)

    assert count == 1
    assert attempts == ["00265-1"]
    assert client.closed_names == ["00265-2"]


def test_uniqueness_conflict_logs_error_when_every_suffix_is_also_rejected(tmp_path):
    poland_org = next(iter(TARGET_ORGANIZATIONS))
    client = FakeMoySkladClient({poland_org: [_shift("s1", "00265")]})
    client.fail_with_uniqueness_ids = {"s1"}
    client.reject_all_renames_ids = {"s1"}
    log = ShiftCloseLog(str(tmp_path / "rename3.sqlite3"))
    close_moment = datetime(2026, 9, 23, 23, 50, tzinfo=timezone.utc)

    run_once(client, log, dry_run=False, close_moment=close_moment)

    assert client.closed == []
    entries = log.recent()
    assert any(e["status"] == "error" and "00265" in e["message"] for e in entries)


def test_a_shift_opened_after_close_moments_day_is_never_touched(tmp_path):
    # exactly the live incident: catching up 2026-09-23's missed close
    # sweeps in a store's brand-new shift opened this morning on 2026-09-24
    # (same LOOKBACK_DAYS window) — it must be skipped entirely, not closed
    # and not renamed.
    poland_org = next(iter(TARGET_ORGANIZATIONS))
    todays_shift = _shift("s-today", "00265")
    todays_shift["moment"] = "2026-09-24 09:59:00.000"
    client = FakeMoySkladClient({poland_org: [todays_shift]})
    log = ShiftCloseLog(str(tmp_path / "later_day.sqlite3"))
    close_moment = datetime(2026, 9, 23, 23, 50, tzinfo=timezone.utc)  # catching up yesterday

    count = run_once(client, log, dry_run=False, close_moment=close_moment)

    assert count == 0
    assert client.closed == []
    entries = log.recent()
    assert entries[0]["kind"] == "run"  # only the summary line — nothing about s-today at all
    assert len(entries) == 1
