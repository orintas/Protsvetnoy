import httpx

from sync_service.ozon_client import OzonClient


def _client_with_handler(handler):
    client = OzonClient(client_id="test-client-id", api_key="test-api-key")
    client._client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-seller.ozon.ru")
    return client


def test_product_offer_ids_paginates_and_skips_archived():
    requests: list[httpx.Request] = []
    pages = [
        {"result": {"items": [{"offer_id": "A1", "archived": False}, {"offer_id": "A2", "archived": True}], "last_id": "page2"}},
        {"result": {"items": [{"offer_id": "A3", "archived": False}], "last_id": ""}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    client = _client_with_handler(handler)
    offer_ids = client.product_offer_ids()

    assert offer_ids == ["A1", "A3"]  # A2 excluded: archived
    assert requests[0].url.path == "/v3/product/list"
    assert b'"last_id":""' in requests[0].content.replace(b" ", b"")
    assert b'"last_id":"page2"' in requests[1].content.replace(b" ", b"")


def test_update_stocks_sends_expected_body():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": []})

    client = _client_with_handler(handler)
    client.update_stocks([{"sku": "A1", "count": 5}], warehouse_id=999)

    assert len(requests) == 1
    assert requests[0].url.path == "/v2/products/stocks"
    body = requests[0].content.replace(b" ", b"")
    assert b'"offer_id":"A1"' in body
    assert b'"stock":5' in body
    assert b'"warehouse_id":999' in body


def test_update_stocks_chunks_at_100_per_request():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": []})

    client = _client_with_handler(handler)
    items = [{"sku": f"SKU{i}", "count": 1} for i in range(150)]
    client.update_stocks(items, warehouse_id=1)

    assert len(requests) == 2  # 100 + 50
