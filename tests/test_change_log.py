import sqlite3
from datetime import datetime, timedelta, timezone

from sync_service.change_log import ChangeLog


def test_add_and_recent_round_trips_before_after(tmp_path):
    log = ChangeLog(str(tmp_path / "changes.sqlite3"))
    log.add(service="shopify", entity_type="product", entity_id="RK018e", action="update",
            before={"title": "old"}, after={"title": "new"}, summary="title changed")

    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["service"] == "shopify"
    assert entries[0]["entity_type"] == "product"
    assert entries[0]["entity_id"] == "RK018e"
    assert entries[0]["action"] == "update"
    assert entries[0]["before"] == '{"title": "old"}'
    assert entries[0]["after"] == '{"title": "new"}'
    assert entries[0]["summary"] == "title changed"


def test_add_allows_missing_before_for_create_actions(tmp_path):
    log = ChangeLog(str(tmp_path / "changes.sqlite3"))
    log.add(service="moysklad", entity_type="customerorder", entity_id="123", action="create", after={"id": "abc"})

    entries = log.recent()
    assert entries[0]["before"] is None
    assert entries[0]["after"] == '{"id": "abc"}'


def test_recent_orders_newest_first(tmp_path):
    log = ChangeLog(str(tmp_path / "changes.sqlite3"))
    log.add(service="ozon", entity_type="posting", entity_id="1", action="ship")
    log.add(service="ozon", entity_type="posting", entity_id="2", action="ship")

    entries = log.recent()
    assert [e["entity_id"] for e in entries] == ["2", "1"]


def test_search_matches_entity_id_or_summary(tmp_path):
    log = ChangeLog(str(tmp_path / "changes.sqlite3"))
    log.add(service="yandex_market", entity_type="offer_stock", entity_id="SKU1", action="update", summary="stock 3->5")
    log.add(service="yandex_market", entity_type="offer_stock", entity_id="SKU2", action="update", summary="stock 0->1")

    assert [e["entity_id"] for e in log.search("SKU1")] == ["SKU1"]
    assert [e["entity_id"] for e in log.search("3->5")] == ["SKU1"]
    assert log.search("nope") == []


def test_purges_entries_older_than_retention_on_open(tmp_path):
    path = str(tmp_path / "changes.sqlite3")
    old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    recent_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE changes (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
            service TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
            action TEXT NOT NULL, before TEXT, after TEXT, summary TEXT)"""
        )
        db.execute("INSERT INTO changes(created_at,service,entity_type,entity_id,action) VALUES (?,'x','x','old','update')", (old_date,))
        db.execute("INSERT INTO changes(created_at,service,entity_type,entity_id,action) VALUES (?,'x','x','recent','update')", (recent_date,))

    log = ChangeLog(path)  # purge runs on open

    entity_ids = {e["entity_id"] for e in log.recent()}
    assert entity_ids == {"recent"}
