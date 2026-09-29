from sync_service.order_assistant import (
    answer_question,
    extract_order_candidates,
    find_order_by_label_message,
    read_order_number_from_image,
    resolve_order_by_number,
    resolve_order_from_text,
)
from sync_service.yandex_market_sync import YandexMarketSyncLog


class FakeMoySklad:
    def __init__(self, by_external_code=None, by_name=None):
        self.by_external_code = by_external_code or {}
        self.by_name = by_name or {}

    def customer_order_by_external_code(self, external_code):
        return self.by_external_code.get(external_code)

    def customer_order_by_name(self, name):
        return self.by_name.get(name)


class FakeAnthropic:
    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls = []

    def complete(self, *, system, user_message, max_tokens=1024, image_bytes=None, image_media_type="image/jpeg"):
        self.calls.append({"system": system, "user_message": user_message, "image_bytes": image_bytes})
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


def test_extract_order_candidates_finds_yandex_market_style_number():
    assert extract_order_candidates("а можно узнать по заказу 62260935105 статус?") == ["62260935105"]


def test_extract_order_candidates_finds_ozon_posting_number():
    assert extract_order_candidates("вот отправление 12345-0001-1 не пришло") == ["12345-0001-1"]


def test_extract_order_candidates_ignores_short_numbers():
    assert extract_order_candidates("заказ 123, розы x5") == []


def test_extract_order_candidates_returns_empty_for_no_digits():
    assert extract_order_candidates("когда обед?") == []


def test_resolve_order_by_number_tries_external_code_then_name():
    order = {"name": "0001234", "description": "Роза x3"}
    moysklad = FakeMoySklad(by_external_code={"999": order})
    assert resolve_order_by_number(candidate="999", moysklad=moysklad) == ("yandex_market", order)

    posting = {"name": "12345-0001-1", "description": "Тюльпан x5"}
    moysklad = FakeMoySklad(by_name={"12345-0001-1": posting})
    assert resolve_order_by_number(candidate="12345-0001-1", moysklad=moysklad) == ("ozon", posting)


def test_resolve_order_by_number_returns_none_when_no_match():
    moysklad = FakeMoySklad()
    assert resolve_order_by_number(candidate="999", moysklad=moysklad) is None


def test_resolve_order_from_text_finds_the_first_matching_candidate():
    order = {"name": "0001234", "description": "Роза x3"}
    moysklad = FakeMoySklad(by_external_code={"62260935105": order})
    assert resolve_order_from_text(text="заказ 62260935105, где он?", moysklad=moysklad) == ("yandex_market", order)


def test_resolve_order_from_text_returns_none_when_nothing_resolves():
    moysklad = FakeMoySklad()
    assert resolve_order_from_text(text="заказ 62260935105, где он?", moysklad=moysklad) is None


def test_read_order_number_from_image_returns_the_number():
    client = FakeAnthropic(reply="62260935105")
    assert read_order_number_from_image(client=client, image_bytes=b"fake-jpeg") == "62260935105"
    assert client.calls[0]["image_bytes"] == b"fake-jpeg"


def test_read_order_number_from_image_returns_none_when_model_finds_nothing():
    client = FakeAnthropic(reply="НЕТ")
    assert read_order_number_from_image(client=client, image_bytes=b"fake-jpeg") is None


def test_answer_question_includes_order_context_and_question():
    order = {"name": "0001234", "state": {"name": "Отгружен"}, "description": "Роза красная x3"}
    client = FakeAnthropic(reply="В заказе 3 розы.")

    result = answer_question(client=client, order=order, marketplace="yandex_market", question="что в заказе?")

    assert result == "В заказе 3 розы."
    user_message = client.calls[0]["user_message"]
    assert "0001234" in user_message
    assert "Отгружен" in user_message
    assert "Роза красная x3" in user_message
    assert "что в заказе?" in user_message
    assert "Яндекс.Маркет" in user_message


def test_answer_question_omits_marketplace_header_when_unknown():
    order = {"name": "0001234", "state": {"name": "Отгружен"}, "description": "Роза x3"}
    client = FakeAnthropic(reply="ок")

    answer_question(client=client, order=order, marketplace=None, question="что в заказе?")

    assert "Площадка" not in client.calls[0]["user_message"]


def test_system_prompt_forbids_claiming_to_cancel():
    from sync_service.order_assistant import SYSTEM_PROMPT

    assert "не можешь отменить" in SYSTEM_PROMPT
