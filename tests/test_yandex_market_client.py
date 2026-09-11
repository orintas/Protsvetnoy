from datetime import datetime

import httpx

from sync_service.yandex_market import YandexMarketClient


def _client_with_handler(handler):
    client = YandexMarketClient(
        base_url="https://api.partner.market.yandex.ru",
        api_key="test-key",
        business_id="939642",
        campaign_id="21924355",
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


def test_update_stocks_builds_sku_payload():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "OK"})

    client = _client_with_handler(handler)
    client.update_stocks([{"sku": "ABC", "count": 5}])
    assert requests[0].url.path == "/v2/campaigns/21924355/offers/stocks"
    body = requests[0].content
    assert b'"sku":"ABC"' in body.replace(b" ", b"")
    assert b'"count":5' in body.replace(b" ", b"")


def test_update_order_status_sends_status_and_substatus():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "OK"})

    client = _client_with_handler(handler)
    client.update_order_status(123, status="PROCESSING", substatus="READY_TO_SHIP")
    assert requests[0].url.path == "/v2/campaigns/21924355/orders/123/status"
    body = requests[0].content.replace(b" ", b"")
    assert b'"status":"PROCESSING"' in body
    assert b'"substatus":"READY_TO_SHIP"' in body
