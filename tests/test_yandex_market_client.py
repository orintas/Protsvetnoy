from datetime import datetime

import httpx

import sync_service.change_log as change_log_module
from sync_service.yandex_market import YandexMarketClient


def _client_with_handler(handler):
    client = YandexMarketClient(
        base_url="https://api.partner.market.yandex.ru",
        api_key="test-key",
        business_id="939642",
    )
    client._client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.partner.market.yandex.ru",
    )
    return client


def test_orders_posts_date_range_and_paginates():
    requests: list[httpx.Request] = []
    pages = [
        {"orders": [{"orderId": 1}], "paging": {"nextPageToken": "next-1"}},
        {"orders": [{"orderId": 2}], "paging": {}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    client = _client_with_handler(handler)
    orders = client.all_orders(
        creation_date_from=datetime(2026, 8, 1),
        creation_date_to=datetime(2026, 8, 29),
    )
    assert [o["orderId"] for o in orders] == [1, 2]
    assert requests[0].url.path == "/v1/businesses/939642/orders"
    assert requests[1].read() and b"next-1" in requests[1].content


def test_campaign_offers_paginates_via_query_string():
    requests: list[httpx.Request] = []
    pages = [
        {"status": "OK", "result": {"offers": [{"offerId": "A"}, {"offerId": "B"}], "paging": {"nextPageToken": "next-1"}}},
        {"status": "OK", "result": {"offers": [{"offerId": "C"}], "paging": {}}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    client = _client_with_handler(handler)
    offer_ids = client.campaign_offers("21924355")
    assert offer_ids == ["A", "B", "C"]
    assert requests[0].url.path == "/v2/campaigns/21924355/offers"
    assert "page_token" not in requests[0].url.params
    assert requests[1].url.params["page_token"] == "next-1"


def test_update_stocks_builds_sku_payload_and_uses_put():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "OK"})

    client = _client_with_handler(handler)
    client.update_stocks([{"sku": "ABC", "count": 5}], campaign_id="21924355", warehouse_id=2346789)
    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/v2/campaigns/21924355/offers/stocks"
    body = requests[0].content.replace(b" ", b"")
    assert b'"sku":"ABC"' in body
    assert b'"warehouseId":2346789' in body
    assert b'"type":"FIT"' in body
    assert b'"count":5' in body


def test_update_order_status_sends_status_and_substatus():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "OK"})

    client = _client_with_handler(handler)
    client.update_order_status(123, campaign_id="21924355", status="PROCESSING", substatus="READY_TO_SHIP")
    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/v2/campaigns/21924355/orders/123/status"
    entries = change_log_module._instance.recent()
    assert len(entries) == 1
    assert entries[0]["service"] == "yandex_market" and entries[0]["entity_id"] == "123"
    body = requests[0].content.replace(b" ", b"")
    assert b'"status":"PROCESSING"' in body
    assert b'"substatus":"READY_TO_SHIP"' in body


def test_order_by_id_filters_by_orderids_and_returns_first_match():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"orders": [{"orderId": 555}]})

    client = _client_with_handler(handler)
    order = client.order_by_id(555)
    assert order == {"orderId": 555}
    assert b'"orderIds":[555]' in requests[0].content.replace(b" ", b"")


def test_order_by_id_returns_none_when_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"orders": []})

    client = _client_with_handler(handler)
    assert client.order_by_id(555) is None


def test_get_order_label_returns_raw_pdf_bytes():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"%PDF-1.4 fake label")

    client = _client_with_handler(handler)
    pdf = client.get_order_label(123, campaign_id="21924355")
    assert pdf == b"%PDF-1.4 fake label"
    assert requests[0].url.path == "/v2/campaigns/21924355/orders/123/delivery/labels"
