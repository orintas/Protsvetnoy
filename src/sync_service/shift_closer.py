from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient

# MoySklad organization id -> country name. Only these four are ever touched;
# everything else (Russia included) is left alone by construction.
TARGET_ORGANIZATIONS: dict[str, str] = {
    "a623f6a9-dde8-11ed-0a80-01540011a4a0": "Польша",
    "147a9f38-1896-11ed-0a80-0e5d000a5470": "Литва",
    "94ad58fb-beec-11ec-0a80-092400291358": "Латвия",
    "0b3fbc43-e0dc-11ec-0a80-0076000f9c69": "Эстония",
}

# All four stores' shifts are closed on one shared clock (Moscow time) rather
# than four separate per-country timezones.
CLOSE_TIMEZONE = ZoneInfo("Europe/Moscow")
CLOSE_HOUR, CLOSE_MINUTE = 23, 50
LOOKBACK_DAYS = 3


class ShiftCloseLog:
    def __init__(self, path: str = "data/shift_close.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS shift_close_log (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, message TEXT NOT NULL, payload TEXT NOT NULL)"""
            )
            db.execute("CREATE TABLE IF NOT EXISTS shift_close_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def add(self, kind: str, status: str, message: str, payload: Any = None) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO shift_close_log(created_at,kind,status,message,payload) VALUES(?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), kind, status, message, json.dumps(payload, ensure_ascii=False, default=str)),
            )

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM shift_close_log ORDER BY id DESC LIMIT ?", (limit,))]

    def clear(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM shift_close_log")

    def last_run_date(self) -> str | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT value FROM shift_close_state WHERE key='last_run_date'").fetchone()
            return row[0] if row else None

    def set_last_run_date(self, value: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO shift_close_state(key,value) VALUES('last_run_date',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (value,),
            )


def _store_name(shift: dict[str, Any]) -> str:
    retail_store = shift.get("retailStore")
    if isinstance(retail_store, dict):
        name = retail_store.get("name")
        if name:
            return str(name)
    return ""


def list_open_shifts(client: MoySkladClient, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Read-only view of currently open PL/LT/LV/EE shifts — never logs or closes anything."""
    now = now or datetime.now(CLOSE_TIMEZONE)
    since = now - timedelta(days=LOOKBACK_DAYS)
    result: list[dict[str, Any]] = []
    for org_id, country in TARGET_ORGANIZATIONS.items():
        for shift in client.open_retail_shifts(org_id, since):
            result.append({
                "country": country,
                "id": shift.get("id"),
                "name": shift.get("name"),
                "opened": shift.get("moment"),
                "store": _store_name(shift),
            })
    return result


def run_once(client: MoySkladClient, log: ShiftCloseLog, *, dry_run: bool, close_moment: datetime) -> int:
    """Check PL/LT/LV/EE stores for unclosed shifts and close them (unless dry_run).

    Returns the number of open shifts found. One shift failing to close does
    not stop the others from being attempted.
    """
    since = close_moment - timedelta(days=LOOKBACK_DAYS)
    close_date = close_moment.strftime("%Y-%m-%d %H:%M:%S.000")
    total_open = 0
    for org_id, country in TARGET_ORGANIZATIONS.items():
        open_shifts = client.open_retail_shifts(org_id, since)
        total_open += len(open_shifts)
        for shift in open_shifts:
            store = _store_name(shift) or "неизвестный магазин"
            info = {
                "id": shift.get("id"),
                "name": shift.get("name"),
                "opened": shift.get("moment"),
                "store": store,
            }
            if dry_run:
                log.add(
                    "shift", "dry-run",
                    f"[{country}] {store}: незакрытая смена №{shift.get('name')} (открыта {shift.get('moment')}) — "
                    "закрытие не выполнено, тестовый режим",
                    info,
                )
                continue
            shift_id = str(shift.get("id"))
            if client.retail_shift_close_date(shift_id):
                log.add(
                    "shift", "success",
                    f"[{country}] {store}: смена №{shift.get('name')} уже закрыта самой кассой — пропущено",
                    info,
                )
                continue
            try:
                client.close_retail_shift(shift_id, close_date)
                log.add(
                    "shift", "success",
                    f"[{country}] {store}: закрыта смена №{shift.get('name')} (была открыта {shift.get('moment')}), "
                    f"дата закрытия {close_date}",
                    info,
                )
            except Exception as error:
                # The pre-check above narrows the race with the store's own POS
                # closing the same shift, but can't fully close it — the PUT
                # itself can still lose that race. Re-check once more before
                # treating this as a real failure, rather than a benign "someone
                # else already closed it" surfacing as a confusing 412 (code 3006).
                if client.retail_shift_close_date(shift_id):
                    log.add(
                        "shift", "success",
                        f"[{country}] {store}: смена №{shift.get('name')} уже закрыта самой кассой — пропущено",
                        info,
                    )
                    continue
                log.add("shift", "error", f"[{country}] {store}: не удалось закрыть смену №{shift.get('name')}: {error}", info)
    suffix = " (тестовый режим — ничего не закрывалось)" if dry_run and total_open else ""
    log.add("run", "success", f"Проверка завершена: незакрытых смен найдено {total_open}{suffix}")
    return total_open


def worker() -> None:
    settings = Settings.from_env()
    log = ShiftCloseLog()
    errors = ErrorLog()
    while True:
        now = datetime.now(CLOSE_TIMEZONE)
        target = now.replace(hour=CLOSE_HOUR, minute=CLOSE_MINUTE, second=0, microsecond=0)
        today = now.date().isoformat()
        if now >= target and log.last_run_date() != today:
            client = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
            try:
                run_once(client, log, dry_run=settings.moysklad_shift_close_dry_run, close_moment=target)
                log.set_last_run_date(today)
            except Exception as error:
                errors.log_exception("moysklad_shift_close_worker", error, context="Ошибка проверки незакрытых смен МойСклад (PL/LT/LV/EE)")
            finally:
                client.close()
        time.sleep(300)
