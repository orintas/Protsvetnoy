from sync_service.http import ApiError
from sync_service.ozon_order_sync import MAX_ATTEMPTS, RFBS_WAREHOUSE_IDS, PendingPostings, handle_webhook_notification, run_once
from sync_service.yandex_market_sync import YandexMarketSyncLog

RFBS_WAREHOUSE_ID = next(iter(RFBS_WAREHOUSE_IDS))
MAIN_WAREHOUSE_ID = 23709754228000  # "Склад Цветной" — plain FBS, not rFBS; deliberately excluded


class FakeSettings:
    ozon_client_id = "test-client-id"
    ozon_api_key = "test-api-key"
    telegram_bot_token = "test-bot-token"
    telegram_proxy_url = ""
    telegram_label_chat_id = "-100123"


class FakeOzon:
    def __init__(self, details_by_posting=None, fail_ship_with=None, fail_label_with=None):
        self.details_by_posting = details_by_posting or {}
        self.fail_ship_with = fail_ship_with or {}
        self.fail_label_with = fail_label_with or {}
        self.ship_calls = []
        self.label_calls = []
        self.closed = False

    def posting_details(self, posting_number):
        return self.details_by_posting.get(posting_number)

    def ship_posting(self, posting_number, products):
        if posting_number in self.fail_ship_with:
            raise ApiError(self.fail_ship_with[posting_number])
        self.ship_calls.append((posting_number, products))

    def package_label(self, posting_numbers):
        posting_number = posting_numbers[0]
        if posting_number in self.fail_label_with:
            raise ApiError(self.fail_label_with[posting_number])
        self.label_calls.append(posting_numbers)
        return b"%PDF-fake"

    def close(self):
        self.closed = True


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_document(self, *, chat_id, document, filename, caption, parse_mode=None):
        self.sent.append((chat_id, document, filename, caption, parse_mode))

    def close(self):
        pass


def _patched(monkeypatch, ozon, telegram=None):
    import sync_service.ozon_order_sync as mod
    monkeypatch.setattr(mod, "OzonClient", lambda **kwargs: ozon)
    if telegram is not None:
        monkeypatch.setattr(mod, "TelegramClient", lambda **kwargs: telegram)


def _posting(status, *, warehouse="ТЦ Саларис", products=None):
    return {
        "status": status,
        "delivery_method": {"warehouse": warehouse},
        "products": products or [{"offer_id": "LE148", "sku": 1536499166, "quantity": 1}],
    }


def test_webhook_ping_returns_ack_payload(tmp_path):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    result = handle_webhook_notification({"message_type": "TYPE_PING"}, queue, log)
    assert result["name"] == "Varvikas sync service"
    assert "version" in result and "time" in result


def test_webhook_new_posting_enqueues_once(tmp_path):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    payload = {"message_type": "TYPE_NEW_POSTING", "posting_number": "X-1", "warehouse_id": RFBS_WAREHOUSE_ID}
    result1 = handle_webhook_notification(payload, queue, log)
    result2 = handle_webhook_notification(payload, queue, log)
    assert result1 == {"result": True}
    assert result2 == {"result": True}
    assert [r["posting_number"] for r in queue.pending()] == ["X-1"]
    assert len(log.recent()) == 1  # second, duplicate webhook did not log again


def test_webhook_new_posting_for_main_warehouse_is_ignored(tmp_path):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    payload = {"message_type": "TYPE_NEW_POSTING", "posting_number": "X-1", "warehouse_id": MAIN_WAREHOUSE_ID}
    result = handle_webhook_notification(payload, queue, log)
    assert result == {"result": True}  # still acked — just not queued
    assert queue.pending() == []
    assert log.recent() == []


def test_webhook_other_types_are_acked_without_side_effects(tmp_path):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    result = handle_webhook_notification({"message_type": "TYPE_NEW_MESSAGE"}, queue, log)
    assert result == {"result": True}
    assert queue.pending() == []
    assert log.recent() == []


def test_run_once_ships_a_posting_still_awaiting_packaging_and_waits_for_label(tmp_path, monkeypatch):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    queue.add("X-1")
    ozon = FakeOzon(details_by_posting={"X-1": _posting("awaiting_packaging")})
    telegram = FakeTelegram()
    _patched(monkeypatch, ozon, telegram)

    run_once(FakeSettings(), queue, log)

    assert ozon.ship_calls == [("X-1", [{"offer_id": "LE148", "sku": 1536499166, "quantity": 1}])]
    assert ozon.label_calls == []  # too early — label not attempted the same tick as shipping
    assert telegram.sent == []
    pending = queue.pending()
    assert len(pending) == 1 and pending[0]["shipped"] == 1 and pending[0]["done"] == 0


def test_run_once_sends_label_once_posting_is_ready(tmp_path, monkeypatch):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    queue.add("X-1")
    ozon = FakeOzon(details_by_posting={"X-1": _posting("awaiting_deliver")})
    telegram = FakeTelegram()
    _patched(monkeypatch, ozon, telegram)

    run_once(FakeSettings(), queue, log)

    assert ozon.ship_calls == []  # already past awaiting_packaging — no ship call needed
    assert ozon.label_calls == [["X-1"]]
    assert telegram.sent[0][0] == "-100123"
    assert telegram.sent[0][3] == "OZON\nТЦ Саларис\nЗаказ №X-1\nLE148 × 1"
    assert telegram.sent[0][4] == "HTML"
    assert queue.pending() == []  # marked done


def test_run_once_retries_when_label_not_ready_yet(tmp_path, monkeypatch):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    queue.add("X-1")
    ozon = FakeOzon(details_by_posting={"X-1": _posting("awaiting_deliver")}, fail_label_with={"X-1": "The next postings aren't ready"})
    telegram = FakeTelegram()
    _patched(monkeypatch, ozon, telegram)

    run_once(FakeSettings(), queue, log)

    assert telegram.sent == []
    pending = queue.pending()
    assert len(pending) == 1 and pending[0]["done"] == 0  # still pending, will retry next tick


def test_run_once_marks_cancelled_posting_done_without_shipping_or_labeling(tmp_path, monkeypatch):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    queue.add("X-1")
    ozon = FakeOzon(details_by_posting={"X-1": _posting("cancelled")})
    telegram = FakeTelegram()
    _patched(monkeypatch, ozon, telegram)

    run_once(FakeSettings(), queue, log)

    assert ozon.ship_calls == [] and ozon.label_calls == [] and telegram.sent == []
    assert queue.pending() == []
    entries = log.recent()
    assert entries[0]["kind"] == "order_cancelled" and entries[0]["status"] == "success"


def test_run_once_gives_up_after_max_attempts(tmp_path, monkeypatch):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    queue.add("X-1")
    ozon = FakeOzon(details_by_posting={"X-1": _posting("awaiting_deliver")}, fail_label_with={"X-1": "not ready"})
    telegram = FakeTelegram()
    _patched(monkeypatch, ozon, telegram)

    for _ in range(MAX_ATTEMPTS + 1):
        run_once(FakeSettings(), queue, log)

    assert queue.pending() == []  # gave up
    error_entries = [e for e in log.recent(limit=1000) if e["kind"] == "order_error"]
    assert len(error_entries) == 1


def test_run_once_with_no_telegram_configured_logs_error_and_stops_retrying(tmp_path, monkeypatch):
    queue = PendingPostings(str(tmp_path / "queue.sqlite3"))
    log = YandexMarketSyncLog(str(tmp_path / "log.sqlite3"))
    queue.add("X-1")
    ozon = FakeOzon(details_by_posting={"X-1": _posting("awaiting_deliver")})
    _patched(monkeypatch, ozon, telegram=None)

    class NoTelegramSettings(FakeSettings):
        telegram_bot_token = ""

    run_once(NoTelegramSettings(), queue, log)

    assert queue.pending() == []
    entries = log.recent()
    assert any(e["kind"] == "label_sent" and e["status"] == "error" for e in entries)
