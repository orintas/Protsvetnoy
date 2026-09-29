from sync_service.error_log import ErrorLog
from sync_service.telegram_qa_worker import UpdateOffset, _is_relevant, run_once


class FakeSettings:
    yandexgpt_api_key = "test-key"
    yandexgpt_folder_id = "test-folder"
    yandexgpt_model = "yandexgpt/latest"
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


class FakeTelegram:
    def __init__(self, updates=None, **kwargs):
        self._updates = updates or []
        self.sent = []
        self.requested_offset = "unset"

    def get_updates(self, *, offset=None, timeout=25, allowed_updates=None):
        self.requested_offset = offset
        return self._updates

    def send_message(self, **kwargs):
        self.sent.append(kwargs)

    def download_file(self, file_id):
        raise AssertionError("no photo in this message")

    def close(self):
        pass


class FakeMoySklad:
    def __init__(self, order=None, **kwargs):
        self._order = order

    def customer_order_by_external_code(self, code):
        return self._order if code == "62260935105" else None

    def customer_order_by_name(self, name):
        return None

    def close(self):
        pass


class FakeLLM:
    def __init__(self, reply="ok", **kwargs):
        self.reply = reply

    def complete(self, **kwargs):
        return self.reply

    def close(self):
        pass


def _install_fakes(monkeypatch, *, telegram, moysklad, llm):
    import sync_service.telegram_qa_worker as mod
    monkeypatch.setattr(mod, "TelegramClient", lambda **kwargs: telegram)
    monkeypatch.setattr(mod, "MoySkladClient", lambda **kwargs: moysklad)
    monkeypatch.setattr(mod, "YandexGPTClient", lambda **kwargs: llm)


def test_is_relevant_accepts_a_human_message_in_the_right_chat():
    message = {"chat": {"id": -100123}, "from": {"is_bot": False}}
    assert _is_relevant(message, FakeSettings()) is True


def test_is_relevant_rejects_messages_from_bots():
    message = {"chat": {"id": -100123}, "from": {"is_bot": True}}
    assert _is_relevant(message, FakeSettings()) is False


def test_is_relevant_rejects_another_chat():
    message = {"chat": {"id": -999}, "from": {"is_bot": False}}
    assert _is_relevant(message, FakeSettings()) is False


def test_update_offset_starts_empty_then_persists(tmp_path):
    store = UpdateOffset(str(tmp_path / "offset.sqlite3"))
    assert store.get() is None
    store.set(42)
    assert store.get() == 42
    store.set(43)
    assert store.get() == 43


def test_run_once_passes_offset_plus_one_to_get_updates(tmp_path, monkeypatch):
    offset_store = UpdateOffset(str(tmp_path / "offset.sqlite3"))
    offset_store.set(10)
    telegram = FakeTelegram(updates=[])
    _install_fakes(monkeypatch, telegram=telegram, moysklad=FakeMoySklad(), llm=FakeLLM())

    run_once(FakeSettings(), offset_store, ErrorLog())

    assert telegram.requested_offset == 11


def test_run_once_drops_a_message_with_no_resolvable_order(tmp_path, monkeypatch):
    offset_store = UpdateOffset(str(tmp_path / "offset.sqlite3"))
    update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": -100123}, "from": {"is_bot": False}, "text": "когда обед?"}}
    telegram = FakeTelegram(updates=[update])
    llm = FakeLLM()

    def _fail_complete(**kwargs):
        raise AssertionError("should not call Claude when no order was found")

    llm.complete = _fail_complete
    _install_fakes(monkeypatch, telegram=telegram, moysklad=FakeMoySklad(), llm=llm)

    run_once(FakeSettings(), offset_store, ErrorLog())

    assert telegram.sent == []
    assert offset_store.get() == 1  # advanced even though nothing was answered


def test_run_once_answers_a_resolvable_order(tmp_path, monkeypatch):
    offset_store = UpdateOffset(str(tmp_path / "offset.sqlite3"))
    update = {"update_id": 7, "message": {"message_id": 5, "chat": {"id": -100123}, "from": {"is_bot": False}, "text": "62260935105 какой статус?"}}
    telegram = FakeTelegram(updates=[update])
    order = {"name": "0001234", "state": {"name": "Отгружен"}, "description": "Роза x3"}
    _install_fakes(monkeypatch, telegram=telegram, moysklad=FakeMoySklad(order=order), llm=FakeLLM(reply="Статус: отгружен."))

    run_once(FakeSettings(), offset_store, ErrorLog())

    assert len(telegram.sent) == 1
    assert telegram.sent[0]["text"] == "Статус: отгружен."
    assert telegram.sent[0]["reply_to_message_id"] == 5
    assert offset_store.get() == 7


def test_run_once_ignores_messages_from_other_chats(tmp_path, monkeypatch):
    offset_store = UpdateOffset(str(tmp_path / "offset.sqlite3"))
    update = {"update_id": 2, "message": {"message_id": 1, "chat": {"id": -999}, "from": {"is_bot": False}, "text": "62260935105"}}
    telegram = FakeTelegram(updates=[update])
    order = {"name": "0001234", "state": {"name": "Отгружен"}, "description": "Роза x3"}
    _install_fakes(monkeypatch, telegram=telegram, moysklad=FakeMoySklad(order=order), llm=FakeLLM())

    run_once(FakeSettings(), offset_store, ErrorLog())

    assert telegram.sent == []
    assert offset_store.get() == 2


def test_run_once_does_nothing_without_a_yandexgpt_key(tmp_path, monkeypatch):
    offset_store = UpdateOffset(str(tmp_path / "offset.sqlite3"))
    telegram = FakeTelegram(updates=[{"update_id": 1, "message": {}}])

    def _fail_if_constructed(**kwargs):
        raise AssertionError("should not build any client when there's no API key")

    import sync_service.telegram_qa_worker as mod
    monkeypatch.setattr(mod, "TelegramClient", _fail_if_constructed)
    monkeypatch.setattr(mod, "MoySkladClient", _fail_if_constructed)
    monkeypatch.setattr(mod, "YandexGPTClient", _fail_if_constructed)

    class NoKeySettings(FakeSettings):
        yandexgpt_api_key = ""

    run_once(NoKeySettings(), offset_store, ErrorLog())

    assert offset_store.get() is None  # never even polled
