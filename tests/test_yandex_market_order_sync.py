from sync_service.yandex_market_order_sync import (
    CAMPAIGN_STORES,
    CANCELLED_STATE_ID,
    COMPLETED_STATE_ID,
    DELIVERING_STATE_ID,
    GROUP_ID,
    MAX_LABEL_RETRIES,
    ORGANIZATION_ID,
    OWNER_EMPLOYEE_ID,
    handle_order_cancelled,
    notify_courier_arrived,
    process_new_order,
    retry_label_if_missing,
    sync_order_delivery_state,
)
from sync_service.yandex_market_sync import YandexMarketSyncLog


class FakeMoySklad:
    def __init__(self, products=None, existing_order=None, order_positions=None, create_loss_error=False):
        self.products = products or {}
        self.existing_order = existing_order
        self.created = None
        self.state_updates = []
        self.order_positions = order_positions if order_positions is not None else [
            {"quantity": 1.0, "assortment": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/prod-1"}}}
        ]
        self.create_loss_error = create_loss_error
        self.losses_created = []

    def customer_order_by_external_code(self, external_code):
        return self.existing_order

    def product_by_code(self, code):
        return self.products.get(code)

    def create_customer_order(self, **kwargs):
        self.created = kwargs
        return {"id": "new-order"}

    def update_customer_order_state(self, order_id, state_id, *, previous_state_id=None):
        self.state_updates.append((order_id, state_id))

    def customer_order_positions(self, order_id):
        return self.order_positions

    def create_loss(self, **kwargs):
        if self.create_loss_error:
            raise RuntimeError("boom loss")
        self.losses_created.append(kwargs)
        return {"id": "loss-1", "name": "8д900", "meta": {"uuidHref": "https://online.moysklad.ru/app/#loss/edit?id=loss-1"}}


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
        self.messages = []
        self.next_message_id = 5000

    def send_document(self, *, chat_id, document, filename, caption, parse_mode=None):
        self.sent.append((chat_id, document, filename, caption, parse_mode))
        self.next_message_id += 1
        return {"ok": True, "result": {"message_id": self.next_message_id}}

    def send_message(self, *, chat_id, text, reply_to_message_id=None, parse_mode=None):
        self.messages.append((chat_id, text, reply_to_message_id, parse_mode))
        return {"ok": True, "result": {"message_id": self.next_message_id + 1}}


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
    assert moysklad.created["description"] == "RGL02 × 1"
    assert moysklad.created["owner_id"] == OWNER_EMPLOYEE_ID
    assert moysklad.created["group_id"] == GROUP_ID

    assert yandex.status_calls == [(999, "149179260", "PROCESSING", "READY_TO_SHIP")]
    assert yandex.label_calls == [(999, "149179260")]
    assert telegram.sent[0][0] == "-100123"
    assert telegram.sent[0][1] == b"%PDF-fake"
    assert telegram.sent[0][3] == "Яндекс.Маркет\nТЦ Ривьера\nЗаказ №999\nRGL02 × 1"
    assert telegram.sent[0][4] == "HTML"


def test_description_lists_each_ordered_sku_with_quantity_on_its_own_line(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    product_a = {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/a", "type": "product"}}
    product_b = {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/b", "type": "product"}}
    moysklad = FakeMoySklad(products={"RGL02": product_a, "LE148": product_b})
    order = _order()
    order["items"] = [
        {"offerId": "RGL02", "count": 2, "prices": {"payment": {"value": 100.0}}},
        {"offerId": "LE148", "count": 1, "prices": {"payment": {"value": 50.0}}},
    ]
    yandex = FakeYandex(order=order)
    process_new_order(order_id=999, campaign_id=149179260, moysklad=moysklad, yandex=yandex, telegram=None, telegram_chat_id="", log=log)

    assert moysklad.created["description"] == "RGL02 × 2\nLE148 × 1"


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


def test_notify_courier_arrived_on_courier_arrived_to_sender_replies_to_label(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    telegram = FakeTelegram()
    log.add("label_sent", "success", "sent earlier", "999", {"message_id": 4242})

    notify_courier_arrived(order_id=999, campaign_id=149179260, status="PROCESSING", substatus="COURIER_ARRIVED_TO_SENDER", telegram=telegram, telegram_chat_id="-100123", log=log)

    assert len(telegram.messages) == 1
    chat_id, text, reply_to, _ = telegram.messages[0]
    assert chat_id == "-100123"
    assert reply_to == 4242
    assert "999" in text and "ТЦ Ривьера" in text


def test_notify_courier_arrived_ignores_other_processing_substatuses(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    telegram = FakeTelegram()

    for substatus in ("STARTED", "READY_TO_SHIP", "COURIER_SEARCH", "COURIER_FOUND"):
        notify_courier_arrived(order_id=999, campaign_id=149179260, status="PROCESSING", substatus=substatus, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert telegram.messages == []


def test_notify_courier_arrived_ignores_later_statuses_too(tmp_path):
    # COURIER_RECEIVED (status DELIVERY) and DELIVERY_SERVICE_DELIVERED
    # (status DELIVERED) both come strictly after the courier already
    # arrived — must not (again) fire on those.
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    telegram = FakeTelegram()

    notify_courier_arrived(order_id=999, campaign_id=149179260, status="DELIVERY", substatus="COURIER_RECEIVED", telegram=telegram, telegram_chat_id="-100123", log=log)
    notify_courier_arrived(order_id=999, campaign_id=149179260, status="DELIVERED", substatus="DELIVERY_SERVICE_DELIVERED", telegram=telegram, telegram_chat_id="-100123", log=log)

    assert telegram.messages == []


def test_notify_courier_arrived_fires_once_per_order(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    telegram = FakeTelegram()

    notify_courier_arrived(order_id=999, campaign_id=149179260, status="PROCESSING", substatus="COURIER_ARRIVED_TO_SENDER", telegram=telegram, telegram_chat_id="-100123", log=log)
    notify_courier_arrived(order_id=999, campaign_id=149179260, status="PROCESSING", substatus="COURIER_ARRIVED_TO_SENDER", telegram=telegram, telegram_chat_id="-100123", log=log)

    assert len(telegram.messages) == 1


def test_notify_courier_arrived_skips_when_telegram_not_configured(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    notify_courier_arrived(order_id=999, campaign_id=149179260, status="PROCESSING", substatus="COURIER_ARRIVED_TO_SENDER", telegram=None, telegram_chat_id="-100123", log=log)
    assert log.recent() == []


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


def test_handle_order_cancelled_moves_moysklad_state_and_notifies_telegram(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()
    log.add("label_sent", "success", "sent earlier", "999", {"message_id": 4242})

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="USER_CHANGED_MIND", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert moysklad.state_updates == [("order-1", CANCELLED_STATE_ID)]
    assert len(telegram.messages) == 1
    chat_id, text, reply_to, _ = telegram.messages[0]
    assert chat_id == "-100123"
    assert reply_to == 4242  # replies to the original label message
    assert "покупатель передумал" in text
    assert "999" in text
    kinds = [e["kind"] for e in log.recent()]
    assert "order_cancelled" in kinds
    assert "cancel_notified" in kinds


def test_handle_order_cancelled_does_not_repeat_on_a_redelivered_webhook(tmp_path):
    """Market can (and does) resend the same ORDER_STATUS_UPDATED webhook —
    previously this function had no idempotency guard on the Telegram step
    at all, so every redelivery sent a second cancellation notice."""
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="USER_CHANGED_MIND", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)
    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="USER_CHANGED_MIND", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert len(telegram.messages) == 1


def test_handle_order_cancelled_is_safe_against_concurrent_redelivery(tmp_path):
    """Drives the exact race with real threads: two near-simultaneous
    deliveries of the same cancellation webhook must not both pass the
    has_success check before either commits it."""
    import threading
    import time

    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()
    real_has_success = log.has_success

    def slow_has_success(kind, external_id):
        # Capture state immediately, then simulate latency before returning
        # it — the same technique used to reproduce the Shopify order race.
        value = real_has_success(kind, external_id)
        time.sleep(0.05)
        return value

    log.has_success = slow_has_success
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="USER_CHANGED_MIND", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(telegram.messages) == 1


def test_handle_order_cancelled_falls_back_to_raw_code_for_unknown_reason(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="SOME_NEW_CODE", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert "SOME_NEW_CODE" in telegram.messages[0][1]


def test_handle_order_cancelled_uses_placeholder_when_no_reason_given(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus=None, moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert "причина не указана" in telegram.messages[0][1]


def test_handle_order_cancelled_without_prior_label_replies_to_nothing(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="USER_CHANGED_MIND", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert telegram.messages[0][2] is None  # no reply_to_message_id on record


def test_handle_order_cancelled_still_notifies_telegram_when_order_missing_from_moysklad(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order=None)
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="USER_CHANGED_MIND", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert moysklad.state_updates == []
    assert len(telegram.messages) == 1  # still worth knowing about, even if MoySklad wasn't updated
    kinds = [e["kind"] for e in log.recent()]
    assert "order_cancel_error" in kinds


def test_handle_order_cancelled_creates_loss_and_notifies_on_shop_failed(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="SHOP_FAILED", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert len(moysklad.losses_created) == 1
    created = moysklad.losses_created[0]
    assert created["organization_id"] == ORGANIZATION_ID
    assert created["store_id"] == CAMPAIGN_STORES["149179260"]
    assert created["positions"] == [{"quantity": 1.0, "assortment": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/prod-1"}}}]
    text = telegram.messages[0][1]
    assert "Создано списание в МойСклад" in text
    assert "8д900" in text
    kinds = [e["kind"] for e in log.recent()]
    assert "loss_created" in kinds


def test_handle_order_cancelled_does_not_create_loss_for_buyer_cancellation(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="USER_CHANGED_MIND", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert moysklad.losses_created == []
    assert "списание" not in telegram.messages[0][1].lower()


def test_handle_order_cancelled_does_not_create_loss_twice_on_redelivered_webhook(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="SHOP_FAILED", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)
    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="SHOP_FAILED", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert len(moysklad.losses_created) == 1


def test_handle_order_cancelled_skips_loss_when_store_unknown_for_campaign(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=999999999, substatus="SHOP_FAILED", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert moysklad.losses_created == []
    assert len(telegram.messages) == 1  # cancellation notice still goes out


def test_handle_order_cancelled_reports_loss_failure_without_blocking_cancellation(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"}, create_loss_error=True)
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="SHOP_FAILED", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert len(telegram.messages) == 1
    assert "Создано списание" not in telegram.messages[0][1]
    kinds = [e["kind"] for e in log.recent()]
    assert "loss_create_error" in kinds
    assert "cancel_notified" in kinds  # still notified despite the loss failure


def test_handle_order_cancelled_skips_loss_when_order_missing_from_moysklad(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order=None)
    telegram = FakeTelegram()

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="SHOP_FAILED", moysklad=moysklad, telegram=telegram, telegram_chat_id="-100123", log=log)

    assert moysklad.losses_created == []


def test_handle_order_cancelled_skips_telegram_when_not_configured(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "ym.sqlite3"))
    moysklad = FakeMoySklad(existing_order={"id": "order-1"})

    handle_order_cancelled(order_id=999, campaign_id=149179260, substatus="USER_CHANGED_MIND", moysklad=moysklad, telegram=None, telegram_chat_id="-100123", log=log)

    assert moysklad.state_updates == [("order-1", CANCELLED_STATE_ID)]  # MoySklad side still happens


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
