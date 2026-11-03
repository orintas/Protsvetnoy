from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sync_service.error_log import ErrorLog
from sync_service.tilda_order_sync import (
    TildaOrderQueue,
    has_valid_key,
    parse_webhook_body,
    process_order,
)
from sync_service.tilda_stock_sync import SITE_STORES
from sync_service.yandex_market_sync import YandexMarketSyncLog


class FakeMoySklad:
    base_url = "https://api.moysklad.ru/api/remap/1.2"

    def __init__(self):
        self.created_order = None
        self.created_counterparty = None

    def customer_order_by_external_code(self, external_code):
        return self.created_order

    def product_by_code(self, code):
        if code != "RB003":
            return None
        return {"meta": {"href": f"{self.base_url}/entity/product/rb003", "type": "product", "mediaType": "application/json"}}

    def counterparty_by_phone(self, phones):
        self.phone_variants = phones
        return None

    def counterparty_by_email(self, email):
        return None

    def create_counterparty(self, **kwargs):
        self.created_counterparty = kwargs
        return {"id": "counterparty-1"}

    def create_customer_order(self, **kwargs):
        self.created_order = {
            "id": "order-1",
            "name": "SO-100",
            "meta": {"uuidHref": f"{self.base_url}/entity/customerorder/order-1"},
        }
        self.created_order_data = kwargs
        return self.created_order


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)


def _settings():
    return type(
        "Settings",
        (),
        {
            "telegram_site_pickup_chat_id": "pickup-chat",
            "telegram_site_delivery_chat_id": "delivery-chat",
            "telegram_label_chat_id": "label-chat",
        },
    )()


def _order_data():
    return {
        "name": "Покупатель",
        "phone": "+7 (999) 123-45-67",
        "email": "buyer@example.com",
        "payment": {
            "orderid": "T-100",
            "amount": "1500",
            "delivery_price": "200",
            "delivery": "Самовывоз: ТЦ Авиапарк",
            "products": [
                {"sku": "RB003", "name": "Оранжерея", "price": "800", "quantity": "2"},
                {"sku": "UNKNOWN", "name": "Не найден", "price": "100", "quantity": "1"},
            ],
        },
    }


def test_parse_webhook_form_with_indexed_products():
    body = urlencode(
        [
            ("payment[orderid]", "T-100"),
            ("payment[products][0][sku]", "RB003"),
            ("payment[products][0][quantity]", "2"),
            ("payment[products][1][sku]", "LG042"),
            ("payment[products][1][quantity]", "1"),
        ]
    ).encode()

    assert parse_webhook_body(body, "application/x-www-form-urlencoded") == {
        "payment": {
            "orderid": "T-100",
            "products": [
                {"sku": "RB003", "quantity": "2"},
                {"sku": "LG042", "quantity": "1"},
            ],
        }
    }


def test_webhook_key_must_match_configured_secret():
    assert has_valid_key({}, {"HTTP_X_TILDA_KEY": "secret"}, "secret")
    assert not has_valid_key({}, {"HTTP_X_TILDA_KEY": "secret"}, "")
    assert not has_valid_key({"api_key": "wrong"}, {}, "secret")


def test_order_queue_deduplicates_and_retries_failed_processing(tmp_path):
    queue = TildaOrderQueue(str(tmp_path / "orders.sqlite3"))
    assert queue.enqueue("T-100", {"payment": {"orderid": "T-100"}})
    assert not queue.enqueue("T-100", {"payment": {"orderid": "T-100"}})

    item = queue.claim("T-100")
    assert item is not None
    assert item["attempts"] == 1
    assert queue.claim("T-100") is None

    queue.finish("T-100", error="temporary failure")
    assert queue.retryable(now=datetime.now(timezone.utc) + timedelta(minutes=3)) == ["T-100"]


def test_process_order_creates_reserved_moysklad_order_and_notifies_once(tmp_path, monkeypatch):
    import sync_service.tilda_order_sync as order_sync

    monkeypatch.setattr(order_sync, "_sales_channel", lambda moysklad: "sales-channel-1")
    moysklad = FakeMoySklad()
    telegram = FakeTelegram()
    log = YandexMarketSyncLog(str(tmp_path / "site.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    data = _order_data()

    process_order(
        order_id="T-100",
        data=data,
        received_at="2026-11-02T10:00:00+00:00",
        moysklad=moysklad,
        telegram=telegram,
        settings=_settings(),
        log=log,
        errors=errors,
    )

    created = moysklad.created_order_data
    assert created["external_code"] == "tilda-T-100"
    assert created["store_id"] == SITE_STORES["aviapark"]
    assert created["positions"][0]["quantity"] == created["positions"][0]["reserve"] == 2
    assert created["positions"][0]["price"] == 80000
    assert created["positions"][0]["discount"] == 23.5294
    assert created["positions"][1]["assortment"]["meta"]["type"] == "service"
    assert "UNKNOWN ×1" in created["description"]
    assert moysklad.created_counterparty["phone"] == "+79991234567"
    assert telegram.messages[0]["chat_id"] == "pickup-chat"
    assert "ТЦ «Авиапарк» — самовывоз" in telegram.messages[0]["text"]
    assert log.has_success("order_notified", "T-100")

    process_order(
        order_id="T-100",
        data=data,
        received_at="2026-11-02T10:00:00+00:00",
        moysklad=moysklad,
        telegram=telegram,
        settings=_settings(),
        log=log,
        errors=errors,
    )

    assert len(telegram.messages) == 1
    assert len(errors.recent()) == 1


def test_notification_falls_back_to_the_label_chat(tmp_path, monkeypatch):
    import sync_service.tilda_order_sync as order_sync

    monkeypatch.setattr(order_sync, "_sales_channel", lambda moysklad: "sales-channel-1")
    telegram = FakeTelegram()
    settings = type("Settings", (), {"telegram_site_pickup_chat_id": "", "telegram_site_delivery_chat_id": "", "telegram_label_chat_id": "label-chat"})()
    process_order(
        order_id="T-100", data=_order_data(), received_at="2026-11-02T10:00:00+00:00", moysklad=FakeMoySklad(), telegram=telegram,
        settings=settings, log=YandexMarketSyncLog(str(tmp_path / "site.sqlite3")), errors=ErrorLog(str(tmp_path / "errors.sqlite3")),
    )
    assert telegram.messages[0]["chat_id"] == "label-chat"
