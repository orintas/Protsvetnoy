import json

from sync_service.yandex_market_sync import YandexMarketSyncLog
from sync_service.yandex_market_webhook import handle_notification, is_allowed_ip, summarize


def test_is_allowed_ip_matches_documented_ranges():
    assert is_allowed_ip("5.45.207.10")
    assert is_allowed_ip("141.8.142.5")
    assert is_allowed_ip("5.255.253.100")


def test_is_allowed_ip_rejects_other_addresses():
    assert not is_allowed_ip("8.8.8.8")
    assert not is_allowed_ip("")
    assert not is_allowed_ip("not-an-ip")


def test_summarize_known_and_unknown_types():
    assert "PING" in summarize({"notificationType": "PING"})
    assert "неизвестный" in summarize({"notificationType": "SOMETHING_NEW"}).lower()


def test_summarize_order_created_shows_store_name_for_a_known_campaign():
    summary = summarize({"notificationType": "ORDER_CREATED", "orderId": 42, "campaignId": 149179260, "items": [{}]})
    assert "42" in summary
    assert "ТЦ Ривьера" in summary
    assert "149179260" not in summary


def test_summarize_order_created_falls_back_to_raw_campaign_id_when_unknown():
    summary = summarize({"notificationType": "ORDER_CREATED", "orderId": 42, "campaignId": 1, "items": [{}]})
    assert "42" in summary
    assert "кампания 1" in summary


def test_handle_notification_logs_every_delivery_without_dedup(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    notification = {"notificationType": "ORDER_STATUS_UPDATED", "orderId": 7, "campaignId": 1, "status": "PROCESSING", "substatus": "STARTED"}
    handle_notification(log, notification)
    handle_notification(log, notification)
    entries = log.recent()
    assert len(entries) == 2
    assert all(e["kind"] == "webhook" for e in entries)
    assert json.loads(entries[0]["payload"])["orderId"] == 7
