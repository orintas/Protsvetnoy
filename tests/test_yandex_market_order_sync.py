from sync_service.yandex_market_order_sync import (
    CAMPAIGN_STORES,
    COMPLETED_STATE_ID,
    DELIVERING_STATE_ID,
    MAX_LABEL_RETRIES,
    process_new_order,
    retry_label_if_missing,
    sync_order_delivery_state,
)
from sync_service.yandex_market_sync import YandexMarketSyncLog


class FakeMoySklad:
    def __init__(self, products=None, existing_order=None):
        self.products = products or {}
        self.existing_order = existing_order
        self.created = None
        self.state_updates = []

    def customer_order_by_external_code(self, external_code):
        return self.existing_order

    def product_by_code(self, code):
        return self.products.get(code)

    def create_customer_order(self, **kwargs):
        self.created = kwargs
        return {"id": "new-order"}

    def update_customer_order_state(self, order_id, state_id):
        self.state_updates.append((order_id, state_id))


class FakeYandex:
    def __init__(self, order=None, fail_status=False, fail_label=False):
        self.order = order
        self.fail_status = fail_status
        self.fail_label = fail_label
        self.status_calls = []
        self.label_calls = []

    def order_by_id(self, order_id):
        return self.order

    def update_order_status(self, order_id, *, campaign_id, status, substatus=None):
        if self.fail_status:
            raise RuntimeError("boom status")
        self.status_calls.append((order_id, campaign_id, status, substatus))

    def get_order_label(self, order_id, *, campaign_id):
        if self.fail_label:
            raise RuntimeError("boom label")
        self.label_calls.append((order_id, campaign_id))
        return b"%PDF-fake"


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_document(self, *, chat_id, document, filename, caption, parse_mode=None):
        self.sent.append((chat_id, document, filename, caption, parse_mode))


def _order(offer_id="RGL02", count=1, price=361300.0, order_id=999, campaign_id="149179260"):
    return {
        "orderId": order_id,
        "campaignId": int(campaign_id),
        "creationDate": "2026-09-13T17:48:46.691+03:00",
        "items": [{"offerId": offer_id, "count": count, "prices": {"payment": {"value": price}}}],
    }


def test_known_campaigns_have_a_store_mapping():
    assert set(CAMPAIGN_STORES) == {"149179204", "149179258", "149179260", "149179270"}


def test_skips_entirely_when_order_and_label_already_done(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    log.add("label_sent", "success", "already sent", "1")
    moysklad = FakeMoySklad(existing_order={"id": "already-there"})
    yandex = FakeYandex()
    process_new_order(order_id=1, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=None, telegram_chat_id="", log=log)
    assert moysklad.created is None
    assert yandex.label_calls == []  # never even asked Market for the label again


def test_resumes_at_label_step_when_order_exists_but_label_was_never_confirmed_sent(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "already-there"})
    yandex = FakeYandex(order=_order())
    telegram = FakeTelegram()
    process_new_order(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert moysklad.created is None  # order creation NOT retried
    assert yandex.status_calls == []  # assembly confirmation NOT retried
    assert yandex.label_calls == [(999, "149179260")]
    assert telegram.sent[0][0] == "-100123"
    kinds = [e["kind"] for e in log.recent()]
    assert kinds == ["label_sent"]


def test_unknown_campaign_logs_error_and_does_nothing(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad()
    yandex = FakeYandex()
    process_new_order(order_id=1, campaign_id=999999, moysklad=moysklad, yandex=yandex, telegram=None, telegram_chat_id="", log=log)
    assert moysklad.created is None
    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["kind"] == "order_pipeline_error"


def test_full_pipeline_creates_confirms_and_sends_label(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    product = {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/abc", "type": "product"}}
    moysklad = FakeMoySklad(products={"RGL02": product})
    yandex = FakeYandex(order=_order())
    telegram = FakeTelegram()
    process_new_order(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert moysklad.created["store_id"] == CAMPAIGN_STORES["149179260"]
    assert moysklad.created["external_code"] == "999"
    assert moysklad.created["positions"] == [{"quantity": 1, "price": 36130000, "assortment": {"meta": product["meta"]}}]

    assert yandex.status_calls == [(999, "149179260", "PROCESSING", "READY_TO_SHIP")]
    assert yandex.label_calls == [(999, "149179260")]
    assert telegram.sent[0][0] == "-100123"
    assert telegram.sent[0][1] == b"%PDF-fake"
    assert telegram.sent[0][3] == "Яндекс.Маркет\nТЦ Ривьера\nЗаказ №999\nRGL02 × 1"
    assert telegram.sent[0][4] == "HTML"


def test_caption_highlights_items_ordered_more_than_once(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    product = {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/abc", "type": "product"}}
    moysklad = FakeMoySklad(products={"RGL02": product})
    order = _order(count=2)
    yandex = FakeYandex(order=order)
    telegram = FakeTelegram()
    process_new_order(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert telegram.sent[0][3] == "Яндекс.Маркет\nТЦ Ривьера\nЗаказ №999\n🔴 <b>RGL02 × 2</b>"
    assert telegram.sent[0][4] == "HTML"

    kinds = [e["kind"] for e in log.recent()]
    assert kinds == ["label_sent", "assembly_confirmed", "order_created"]


def test_missing_product_code_is_logged_and_order_still_created_for_the_rest(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    product = {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/abc", "type": "product"}}
    order = _order()
    order["items"].append({"offerId": "MISSING1", "count": 1, "prices": {"payment": {"value": 100.0}}})
    moysklad = FakeMoySklad(products={"RGL02": product})
    yandex = FakeYandex(order=order)
    process_new_order(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=None, telegram_chat_id="", log=log)

    assert moysklad.created is not None
    assert len(moysklad.created["positions"]) == 1
    error_entries = [e for e in log.recent() if e["kind"] == "order_pipeline_error"]
    assert any("MISSING1" in e["message"] for e in error_entries)


def test_no_matching_products_at_all_skips_order_creation(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(products={})
    yandex = FakeYandex(order=_order())
    process_new_order(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=None, telegram_chat_id="", log=log)
    assert moysklad.created is None


def test_telegram_not_configured_logs_error_but_does_not_raise(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    product = {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/abc", "type": "product"}}
    moysklad = FakeMoySklad(products={"RGL02": product})
    yandex = FakeYandex(order=_order())
    process_new_order(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=None, telegram_chat_id="", log=log)
    label_entries = [e for e in log.recent() if e["kind"] == "label_sent"]
    assert label_entries[0]["status"] == "error"


def test_delivery_status_sets_delivering_state(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    sync_order_delivery_state(order_id=999, status="DELIVERY", substatus=None, moysklad=moysklad, log=log)
    assert moysklad.state_updates == [("order-1", DELIVERING_STATE_ID)]
    assert log.recent()[0]["kind"] == "order_state_updated"


def test_delivery_service_received_substatus_also_sets_delivering_state(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    sync_order_delivery_state(order_id=999, status="PROCESSING", substatus="DELIVERY_SERVICE_RECEIVED", moysklad=moysklad, log=log)
    assert moysklad.state_updates == [("order-1", DELIVERING_STATE_ID)]


def test_delivered_status_sets_completed_state(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    sync_order_delivery_state(order_id=999, status="DELIVERED", substatus=None, moysklad=moysklad, log=log)
    assert moysklad.state_updates == [("order-1", COMPLETED_STATE_ID)]


def test_unrelated_status_is_ignored(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    sync_order_delivery_state(order_id=999, status="PROCESSING", substatus="READY_TO_SHIP", moysklad=moysklad, log=log)
    assert moysklad.state_updates == []
    assert log.recent() == []


def test_delivery_status_for_unknown_order_logs_error_without_crashing(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order=None)
    sync_order_delivery_state(order_id=999, status="DELIVERED", substatus=None, moysklad=moysklad, log=log)
    assert moysklad.state_updates == []
    assert log.recent()[0]["kind"] == "order_state_error"


def test_retry_label_sends_it_when_order_exists_but_label_never_confirmed(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "already-there"})
    yandex = FakeYandex(order=_order())
    telegram = FakeTelegram()
    retry_label_if_missing(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=telegram, telegram_chat_id="-100123", log=log)
    assert yandex.label_calls == [(999, "149179260")]
    assert telegram.sent[0][0] == "-100123"
    assert log.has_success("label_sent", "999")


def test_retry_label_does_nothing_once_already_confirmed_sent(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    log.add("label_sent", "success", "already sent", "999")
    moysklad = FakeMoySklad(existing_order={"id": "already-there"})
    yandex = FakeYandex(order=_order())
    retry_label_if_missing(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=FakeTelegram(), telegram_chat_id="-100123", log=log)
    assert yandex.label_calls == []


def test_retry_label_does_nothing_when_order_was_never_created(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order=None)
    yandex = FakeYandex(order=_order())
    retry_label_if_missing(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=FakeTelegram(), telegram_chat_id="-100123", log=log)
    assert yandex.label_calls == []


def test_retry_label_stops_after_max_attempts(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "already-there"})
    yandex = FakeYandex(order=_order(), fail_label=True)
    for _ in range(MAX_LABEL_RETRIES + 2):
        retry_label_if_missing(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=FakeTelegram(), telegram_chat_id="-100123", log=log)
    assert yandex.label_calls == []  # get_order_label always raised, so no send ever happened
    errors = [e for e in log.recent() if e["kind"] == "label_retry_error"]
    assert len(errors) == MAX_LABEL_RETRIES
    assert not log.has_success("label_sent", "999")
