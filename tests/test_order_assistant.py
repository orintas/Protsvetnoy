from sync_service.order_assistant import (
    answer_question,
    extract_order_candidates,
    fetch_marketplace_details,
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


class FakeYandex:
    def __init__(self, order=None, error=False):
        self.order = order
        self.error = error
        self.requested_order_id = None

    def order_by_id(self, order_id):
        self.requested_order_id = order_id
        if self.error:
            raise RuntimeError("boom")
        return self.order


class FakeOzon:
    def __init__(self, details=None, error=False):
        self.details = details
        self.error = error
        self.requested_posting_number = None

    def posting_details(self, posting_number):
        self.requested_posting_number = posting_number
        if self.error:
            raise RuntimeError("boom")
        return self.details


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


def test_extract_order_candidates_finds_ozon_posting_number_with_a_long_prefix():
    """Real posting numbers regularly have an 8-10 digit prefix (e.g.
    "87792534-0050-1", "0113798402-0282-1") — a previous tight upper bound
    on the first digit group made the regex match a truncated tail instead
    of the full number, silently breaking resolution for most real OZON
    orders. Confirmed live 2026-09-30: 13 of 14 real posting numbers
    extracted wrong."""
    candidates = extract_order_candidates("🚚 Приехал курьер в ТЦ Ривьера за заказом 87792534-0050-1")
    assert candidates[0] == "87792534-0050-1"

    candidates = extract_order_candidates("отправление 0113798402-0282-1 готово")
    assert candidates[0] == "0113798402-0282-1"


def test_extract_order_candidates_ignores_short_numbers():
    assert extract_order_candidates("заказ 123, розы x5") == []


def test_extract_order_candidates_returns_empty_for_no_digits():
    assert extract_order_candidates("когда обед?") == []


def test_resolve_order_by_number_tries_external_code_then_name():
    order = {"name": "0001234", "description": "Роза x3"}
    moysklad = FakeMoySklad(by_external_code={"999": order})
    assert resolve_order_by_number(candidate="999", moysklad=moysklad) == ("yandex_market", "999", order)

    posting = {"name": "12345-0001-1", "description": "Тюльпан x5"}
    moysklad = FakeMoySklad(by_name={"12345-0001-1": posting})
    assert resolve_order_by_number(candidate="12345-0001-1", moysklad=moysklad) == ("ozon", "12345-0001-1", posting)


def test_resolve_order_by_number_returns_none_when_no_match():
    moysklad = FakeMoySklad()
    assert resolve_order_by_number(candidate="999", moysklad=moysklad) is None


def test_resolve_order_from_text_finds_the_first_matching_candidate():
    order = {"name": "0001234", "description": "Роза x3"}
    moysklad = FakeMoySklad(by_external_code={"62260935105": order})
    assert resolve_order_from_text(text="заказ 62260935105, где он?", moysklad=moysklad) == ("yandex_market", "62260935105", order)


def test_resolve_order_from_text_resolves_a_reply_to_a_courier_notice():
    """The real reported scenario: replying to the bot's own courier-arrival
    message ("🚚 Приехал курьер в ... за заказом 87792534-0050-1"), whose
    text ends up in the search text via the reply_to merge in
    telegram_qa_worker. Must resolve the full posting number, not a
    truncated prefix."""
    posting = {"name": "87792534-0050-1", "description": "Мозаика x1"}
    moysklad = FakeMoySklad(by_name={"87792534-0050-1": posting})
    result = resolve_order_from_text(text="а что там? 🚚 Приехал курьер в Экспресс_ТЦ_Авиапарк за заказом 87792534-0050-1", moysklad=moysklad)
    assert result == ("ozon", "87792534-0050-1", posting)


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


def test_fetch_marketplace_details_calls_yandex_order_by_id():
    yandex = FakeYandex(order={"status": "DELIVERY"})
    result = fetch_marketplace_details(marketplace="yandex_market", external_id="999", yandex=yandex, ozon=None)
    assert result == {"status": "DELIVERY"}
    assert yandex.requested_order_id == 999


def test_fetch_marketplace_details_calls_ozon_posting_details():
    ozon = FakeOzon(details={"status": "awaiting_deliver"})
    result = fetch_marketplace_details(marketplace="ozon", external_id="12345-0001-1", yandex=None, ozon=ozon)
    assert result == {"status": "awaiting_deliver"}
    assert ozon.requested_posting_number == "12345-0001-1"


def test_fetch_marketplace_details_returns_none_on_error():
    yandex = FakeYandex(error=True)
    assert fetch_marketplace_details(marketplace="yandex_market", external_id="999", yandex=yandex, ozon=None) is None


def test_fetch_marketplace_details_returns_none_when_client_missing():
    assert fetch_marketplace_details(marketplace="yandex_market", external_id="999", yandex=None, ozon=None) is None


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


def test_answer_question_includes_yandex_market_courier_details():
    order = {"name": "0001234", "state": {"name": "Отгружен"}, "description": "Роза x3"}
    details = {
        "status": "DELIVERY",
        "substatus": "COURIER_RECEIVED",
        "delivery": {"transfer": {"courier": {"fullName": "Иван Иванов", "phone": "+79991234567", "vehicleDescription": "Hyundai белый", "vehicleNumber": "А123БВ777"}}},
    }
    client = FakeAnthropic(reply="ок")

    answer_question(client=client, order=order, marketplace="yandex_market", question="какой телефон у курьера?", marketplace_details=details)

    user_message = client.calls[0]["user_message"]
    assert "Иван Иванов" in user_message
    assert "+79991234567" in user_message
    assert "DELIVERY/COURIER_RECEIVED" in user_message


def test_answer_question_includes_ozon_courier_details():
    order = {"name": "12345-0001-1", "state": {"name": "Отгружен"}, "description": "Тюльпан x5"}
    details = {
        "status": "awaiting_deliver",
        "provider_status": "Курьер у продавца",
        "courier": {"name": "Пётр Петров", "phone": "+79991112233", "car_model": "Hyundai Getz", "car_number": "Р585АТ159"},
    }
    client = FakeAnthropic(reply="ок")

    answer_question(client=client, order=order, marketplace="ozon", question="где курьер?", marketplace_details=details)

    user_message = client.calls[0]["user_message"]
    assert "Пётр Петров" in user_message
    assert "+79991112233" in user_message
    assert "Курьер у продавца" in user_message


def test_answer_question_skips_marketplace_lines_when_details_missing():
    order = {"name": "0001234", "state": {"name": "Отгружен"}, "description": "Роза x3"}
    client = FakeAnthropic(reply="ок")

    answer_question(client=client, order=order, marketplace="yandex_market", question="что в заказе?", marketplace_details=None)

    assert "Статус на Яндекс.Маркете" not in client.calls[0]["user_message"]


def test_answer_question_uses_customer_order_by_name_for_ozon():
    order = {"name": "12345-0001-1", "state": {"name": "Отгружен"}, "description": "Тюльпан x5"}
    client = FakeAnthropic(reply="5 тюльпанов.")

    result = answer_question(client=client, order=order, marketplace="ozon", question="сколько тюльпанов?")

    assert result == "5 тюльпанов."


def test_system_prompt_forbids_claiming_to_act():
    from sync_service.order_assistant import SYSTEM_PROMPT

    assert "не можешь ничего изменить" in SYSTEM_PROMPT
