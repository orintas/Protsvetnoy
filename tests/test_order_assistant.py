from sync_service.order_assistant import (
    answer_question,
    extract_order_candidates,
    fetch_marketplace_details,
    fetch_stock_context,
    find_order_by_label_message,
    read_order_number_from_image,
    resolve_order_by_number,
    resolve_order_from_text,
)
from sync_service.yandex_market_sync import YandexMarketSyncLog


class FakeMoySklad:
    def __init__(self, by_external_code=None, by_name=None, positions=None, stock_rows=None):
        self.by_external_code = by_external_code or {}
        self.by_name = by_name or {}
        self.positions = positions or []
        self.stock_rows = stock_rows or []
        self.requested_order_id = None
        self.requested_store_id = None
        self.requested_product_ids = None

    def customer_order_by_external_code(self, external_code):
        return self.by_external_code.get(external_code)

    def customer_order_by_name(self, name):
        return self.by_name.get(name)

    def customer_order_positions(self, order_id):
        self.requested_order_id = order_id
        return self.positions

    def stock_by_products(self, product_ids, *, store_id):
        self.requested_product_ids = product_ids
        self.requested_store_id = store_id
        return self.stock_rows


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


def test_extract_order_candidates_finds_number_right_after_a_typed_numero_sign():
    """Real reported message: "Nº62397236160" — "º" (U+00BA MASCULINE
    ORDINAL INDICATOR, how "№" often gets typed from a Latin keyboard) is a
    Unicode letter, so a plain \\b word boundary saw no boundary between it
    and the digits and the match silently failed entirely. Confirmed live
    2026-09-30."""
    assert extract_order_candidates("Бот, дай инфу по заказу Nº62397236160") == ["62397236160"]


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


def _order_with_store(store_id="store-1", order_id="order-1", name="0001234"):
    return {
        "id": order_id,
        "name": name,
        "state": {"name": "Отгружен"},
        "description": "Картина MG2466 x1",
        "store": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/store/{store_id}"}},
    }


def test_fetch_stock_context_includes_current_moysklad_stock(tmp_path):
    order = _order_with_store()
    moysklad = FakeMoySklad(
        positions=[{"assortment": {"id": "prod-1", "code": "MG2466"}}],
        stock_rows=[{"code": "MG2466", "stock": 1, "reserve": 1, "quantity": 0}],
    )
    yandex_log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))

    lines = fetch_stock_context(moysklad=moysklad, yandex_log=yandex_log, order=order, marketplace=None, external_id=None)

    assert moysklad.requested_order_id == "order-1"
    assert moysklad.requested_store_id == "store-1"
    assert moysklad.requested_product_ids == ["prod-1"]
    assert any("MG2466" in line and "резерв 1" in line and "доступно 0" in line for line in lines)


def test_fetch_stock_context_reports_zero_when_product_missing_from_stock_rows(tmp_path):
    order = _order_with_store()
    moysklad = FakeMoySklad(positions=[{"assortment": {"id": "prod-1", "code": "MG2466"}}], stock_rows=[])
    yandex_log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))

    lines = fetch_stock_context(moysklad=moysklad, yandex_log=yandex_log, order=order, marketplace=None, external_id=None)

    assert any("MG2466" in line and "0 шт." in line for line in lines)


def test_fetch_stock_context_includes_closest_yandex_market_history_entry(tmp_path):
    order = _order_with_store()
    moysklad = FakeMoySklad(positions=[{"assortment": {"id": "prod-1", "code": "MG2466"}}], stock_rows=[{"code": "MG2466", "stock": 0, "reserve": 0, "quantity": 0}])
    yandex_log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    yandex_log.add("order_created", "success", "Заказ создан", "999", None)
    with __import__("sqlite3").connect(yandex_log.path) as db:
        db.execute("UPDATE sync_log SET created_at=? WHERE kind='order_created'", ("2026-10-05T07:34:00+00:00",))
        db.execute(
            "INSERT INTO sync_log(created_at,kind,external_id,status,message,payload) VALUES (?,?,?,?,?,?)",
            ("2026-10-05T06:04:29+00:00", "stock_sync", None, "success", "ТЦ Саларис: MG2466: 1→0", "{}"),
        )
        db.execute(
            "INSERT INTO sync_log(created_at,kind,external_id,status,message,payload) VALUES (?,?,?,?,?,?)",
            ("2026-10-06T06:04:29+00:00", "stock_sync", None, "success", "ТЦ Саларис: MG2466: 0→5", "{}"),
        )

    lines = fetch_stock_context(moysklad=moysklad, yandex_log=yandex_log, order=order, marketplace="yandex_market", external_id="999")

    joined = "\n".join(lines)
    assert "1→0" in joined
    assert "0→5" not in joined  # that entry is after the order, not the closest one before it


def test_fetch_stock_context_skips_yandex_market_history_for_ozon(tmp_path):
    order = _order_with_store()
    moysklad = FakeMoySklad(positions=[{"assortment": {"id": "prod-1", "code": "MG2466"}}], stock_rows=[{"code": "MG2466", "stock": 1, "reserve": 0, "quantity": 1}])
    yandex_log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    yandex_log.add("stock_sync", "success", "ТЦ Саларис: MG2466: 1→0", None, None)

    lines = fetch_stock_context(moysklad=moysklad, yandex_log=yandex_log, order=order, marketplace="ozon", external_id="12345-0001-1")

    assert "Яндекс.Маркет" not in "\n".join(lines)


def test_fetch_stock_context_includes_ozon_stock_history(tmp_path):
    order = _order_with_store()
    moysklad = FakeMoySklad(positions=[{"assortment": {"id": "prod-1", "code": "MG2466"}}], stock_rows=[{"code": "MG2466", "stock": 0, "reserve": 0, "quantity": 0}])
    ozon_log = YandexMarketSyncLog(str(tmp_path / "ozon.sqlite3"))
    ozon_log.add("order_created", "success", "Отправление 12345-0001-1: упаковка подтверждена", "12345-0001-1", None)
    # OZON logs one row per warehouse covering several SKUs — the specific
    # SKU's before/after lives in payload["changes"], not necessarily spelled
    # out in the summary message text.
    ozon_log.add(
        "ozon_stock_sync", "success",
        "OZON ТЦ Саларис: остатки обновлены, офферов 500, изменилось 2: MG2466: 1→0 и ещё 1",
        None, {"warehouse_id": 1, "count": 500, "changes": [{"sku": "MG2466", "before": 1, "after": 0}, {"sku": "OTHER", "before": 2, "after": 1}]},
    )

    lines = fetch_stock_context(moysklad=moysklad, yandex_log=YandexMarketSyncLog(str(tmp_path / "ym.sqlite3")), ozon_log=ozon_log, order=order, marketplace="ozon", external_id="12345-0001-1")

    joined = "\n".join(lines)
    assert "OZON" in joined
    assert "MG2466: 1→0" in joined


def test_fetch_stock_context_returns_empty_when_order_has_no_store(tmp_path):
    order = {"id": "order-1", "name": "0001234", "description": "x"}
    moysklad = FakeMoySklad()
    yandex_log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))

    assert fetch_stock_context(moysklad=moysklad, yandex_log=yandex_log, order=order, marketplace=None, external_id=None) == []


def test_answer_question_includes_stock_lines():
    order = {"name": "0001234", "state": {"name": "Отгружен"}, "description": "Картина MG2466 x1"}
    client = FakeAnthropic(reply="На складе нет.")

    answer_question(
        client=client, order=order, marketplace="yandex_market", question="есть ли остаток?",
        stock_lines=["Текущий остаток в МойСклад на складе заказа:", "MG2466: остаток 1, резерв 1, доступно 0"],
    )

    user_message = client.calls[0]["user_message"]
    assert "MG2466: остаток 1, резерв 1, доступно 0" in user_message
