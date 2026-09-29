from sync_service.error_log import ErrorLog
from sync_service.telegram_qa_worker import PendingTelegramMessages, enqueue_if_relevant, run_once


class FakeSettings:
    anthropic_api_key = "test-key"
    anthropic_model = "claude-sonnet-5"
    telegram_label_chat_id = "-100123"
    telegram_bot_token = "bot-token"
    telegram_proxy_url = ""
    moysklad_base_url = "https://x"
    moysklad_token = "x"
    yandex_market_base_url = "https://x"
    yandex_market_api_key = ""
    yandex_market_business_id = ""
    ozon_client_id = ""
    ozon_api_key = ""


def test_enqueue_if_relevant_adds_a_matching_message(tmp_path):
    queue = PendingTelegramMessages(str(tmp_path / "q.sqlite3"))
    message = {"message_id": 1, "chat": {"id": -100123}, "text": "62260935105", "from": {"is_bot": False}}
    enqueue_if_relevant({"message": message}, settings=FakeSettings(), queue=queue)
    assert len(queue.pending()) == 1


def test_enqueue_if_relevant_skips_when_no_api_key(tmp_path):
    queue = PendingTelegramMessages(str(tmp_path / "q.sqlite3"))
    message = {"message_id": 1, "chat": {"id": -100123}, "text": "62260935105", "from": {"is_bot": False}}

    class NoKeySettings(FakeSettings):
        anthropic_api_key = ""

    enqueue_if_relevant({"message": message}, settings=NoKeySettings(), queue=queue)
    assert queue.pending() == []


def test_enqueue_if_relevant_skips_messages_from_bots(tmp_path):
    queue = PendingTelegramMessages(str(tmp_path / "q.sqlite3"))
    message = {"message_id": 1, "chat": {"id": -100123}, "text": "62260935105", "from": {"is_bot": True}}
    enqueue_if_relevant({"message": message}, settings=FakeSettings(), queue=queue)
    assert queue.pending() == []


def test_enqueue_if_relevant_skips_messages_from_another_chat(tmp_path):
    queue = PendingTelegramMessages(str(tmp_path / "q.sqlite3"))
    message = {"message_id": 1, "chat": {"id": -999}, "text": "62260935105", "from": {"is_bot": False}}
    enqueue_if_relevant({"message": message}, settings=FakeSettings(), queue=queue)
    assert queue.pending() == []


def test_enqueue_if_relevant_skips_non_message_updates(tmp_path):
    queue = PendingTelegramMessages(str(tmp_path / "q.sqlite3"))
    enqueue_if_relevant({"edited_message": {}}, settings=FakeSettings(), queue=queue)
    assert queue.pending() == []


def test_pending_messages_queue_marks_done(tmp_path):
    queue = PendingTelegramMessages(str(tmp_path / "q.sqlite3"))
    queue.add({"message_id": 1, "text": "hi"})
    [row] = queue.pending()
    queue.mark_done(row["id"])
    assert queue.pending() == []


def test_run_once_drops_a_message_with_no_resolvable_order(tmp_path, monkeypatch):
    queue = PendingTelegramMessages(str(tmp_path / "q.sqlite3"))
    queue.add({"message_id": 1, "chat": {"id": -100123}, "text": "когда обед?"})

    import sync_service.telegram_qa_worker as mod

    # MoySklad/Telegram/Anthropic clients are constructed unconditionally by
    # run_once (cheap, no network I/O on construction) — only assert no
    # *answer* is sent and Claude is never called for an unresolvable message.
    sent = []

    class FakeTelegram:
        def __init__(self, **kwargs):
            pass

        def send_message(self, **kwargs):
            sent.append(kwargs)

        def download_file(self, file_id):
            raise AssertionError("no photo in this message")

        def close(self):
            pass

    class FakeMoySklad:
        def __init__(self, **kwargs):
            pass

        def customer_order_by_external_code(self, code):
            return None

        def customer_order_by_name(self, name):
            return None

        def close(self):
            pass

    class FakeAnthropic:
        def __init__(self, **kwargs):
            pass

        def complete(self, **kwargs):
            raise AssertionError("should not call Claude when no order was found")

        def close(self):
            pass

    monkeypatch.setattr(mod, "TelegramClient", FakeTelegram)
    monkeypatch.setattr(mod, "MoySkladClient", FakeMoySklad)
    monkeypatch.setattr(mod, "AnthropicClient", FakeAnthropic)

    run_once(FakeSettings(), queue, ErrorLog())

    assert sent == []
    assert queue.pending() == []  # still marked done, not retried forever


def test_run_once_answers_a_resolvable_order(tmp_path, monkeypatch):
    queue = PendingTelegramMessages(str(tmp_path / "q.sqlite3"))
    queue.add({"message_id": 5, "chat": {"id": -100123}, "text": "62260935105 какой статус?"})

    import sync_service.telegram_qa_worker as mod

    sent = []

    class FakeTelegram:
        def __init__(self, **kwargs):
            pass

        def send_message(self, **kwargs):
            sent.append(kwargs)

        def download_file(self, file_id):
            raise AssertionError("no photo in this message")

        def close(self):
            pass

    order = {"name": "0001234", "state": {"name": "Отгружен"}, "description": "Роза x3"}

    class FakeMoySklad:
        def __init__(self, **kwargs):
            pass

        def customer_order_by_external_code(self, code):
            return order if code == "62260935105" else None

        def customer_order_by_name(self, name):
            return None

        def close(self):
            pass

    class FakeAnthropic:
        def __init__(self, **kwargs):
            pass

        def complete(self, **kwargs):
            return "Статус: отгружен."

        def close(self):
            pass

    monkeypatch.setattr(mod, "TelegramClient", FakeTelegram)
    monkeypatch.setattr(mod, "MoySkladClient", FakeMoySklad)
    monkeypatch.setattr(mod, "AnthropicClient", FakeAnthropic)

    run_once(FakeSettings(), queue, ErrorLog())

    assert len(sent) == 1
    assert sent[0]["text"] == "Статус: отгружен."
    assert sent[0]["reply_to_message_id"] == 5
    assert queue.pending() == []
