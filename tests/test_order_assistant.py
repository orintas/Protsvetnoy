from sync_service.order_assistant import answer_question, find_order_by_label_message
from sync_service.yandex_market_sync import YandexMarketSyncLog


class FakeMoySklad:
    def __init__(self, order_by_external_code=None, order_by_name=None):
        self.order_by_external_code = order_by_external_code
        self.order_by_name = order_by_name

    def customer_order_by_external_code(self, external_code):
        return self.order_by_external_code

    def customer_order_by_name(self, name):
        return self.order_by_name


class FakeAnthropic:
    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls = []

    def complete(self, *, system, user_message, max_tokens=1024):
        self.calls.append({"system": system, "user_message": user_message})
        return self.reply


def test_find_order_by_label_message_resolves_yandex_market(tmp_path):
    ym_log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    ozon_log = YandexMarketSyncLog(str(tmp_path / "ozon.sqlite3"))
    ym_log.add("label_sent", "success", "sent", "999", {"message_id": 42})

    assert find_order_by_label_message(reply_to_message_id=42, yandex_log=ym_log, ozon_log=ozon_log) == ("yandex_market", "999")


def test_find_order_by_label_message_resolves_ozon(tmp_path):
    ym_log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    ozon_log = YandexMarketSyncLog(str(tmp_path / "ozon.sqlite3"))
    ozon_log.add("label_sent", "success", "sent", "12345-0001-1", {"message_id": 77})

    assert find_order_by_label_message(reply_to_message_id=77, yandex_log=ym_log, ozon_log=ozon_log) == ("ozon", "12345-0001-1")


def test_find_order_by_label_message_returns_none_for_unknown_reply(tmp_path):
    ym_log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    ozon_log = YandexMarketSyncLog(str(tmp_path / "ozon.sqlite3"))

    assert find_order_by_label_message(reply_to_message_id=1, yandex_log=ym_log, ozon_log=ozon_log) is None


def test_answer_question_returns_none_when_order_missing_from_moysklad():
    moysklad = FakeMoySklad(order_by_external_code=None)
    client = FakeAnthropic()

    result = answer_question(client=client, marketplace="yandex_market", external_id="999", moysklad=moysklad, question="где заказ?")

    assert result is None
    assert client.calls == []


def test_answer_question_includes_order_context_and_question():
    order = {"name": "0001234", "state": {"name": "Отгружен"}, "description": "Роза красная x3"}
    moysklad = FakeMoySklad(order_by_external_code=order)
    client = FakeAnthropic(reply="В заказе 3 розы.")

    result = answer_question(client=client, marketplace="yandex_market", external_id="999", moysklad=moysklad, question="что в заказе?")

    assert result == "В заказе 3 розы."
    assert len(client.calls) == 1
    user_message = client.calls[0]["user_message"]
    assert "0001234" in user_message
    assert "Отгружен" in user_message
    assert "Роза красная x3" in user_message
    assert "что в заказе?" in user_message


def test_answer_question_uses_customer_order_by_name_for_ozon():
    order = {"name": "12345-0001-1", "state": {"name": "Отгружен"}, "description": "Тюльпан x5"}
    moysklad = FakeMoySklad(order_by_name=order)
    client = FakeAnthropic(reply="5 тюльпанов.")

    result = answer_question(client=client, marketplace="ozon", external_id="12345-0001-1", moysklad=moysklad, question="сколько тюльпанов?")

    assert result == "5 тюльпанов."


def test_system_prompt_forbids_claiming_to_cancel():
    from sync_service.order_assistant import SYSTEM_PROMPT

    assert "не можешь отменить" in SYSTEM_PROMPT
