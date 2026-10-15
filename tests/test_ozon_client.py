import httpx

import sync_service.change_log as change_log_module
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


def test_posting_details_returns_result_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/posting/fbs/get"
        return httpx.Response(200, json={"result": {"posting_number": "X-1", "status": "awaiting_packaging"}})

    client = _client_with_handler(handler)
    result = client.posting_details("X-1")
    assert result == {"posting_number": "X-1", "status": "awaiting_packaging"}


def test_posting_details_returns_none_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = _client_with_handler(handler)
    assert client.posting_details("missing") is None


def test_ship_posting_sends_products_as_packages():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": {}})

    client = _client_with_handler(handler)
    client.ship_posting("X-1", [{"sku": 123, "quantity": 2}])

    assert requests[0].url.path == "/v4/posting/fbs/ship"
    body = requests[0].content.replace(b" ", b"")
    assert b'"posting_number":"X-1"' in body
    assert b'"product_id":123' in body
    assert b'"quantity":2' in body


def test_package_label_returns_raw_pdf_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/posting/fbs/package-label"
        return httpx.Response(200, content=b"%PDF-fake", headers={"content-type": "application/pdf"})

    client = _client_with_handler(handler)
    assert client.package_label(["X-1"]) == b"%PDF-fake"


def test_ship_posting_records_change_log_entry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {}})

    client = _client_with_handler(handler)
    client.ship_posting("X-1", [{"sku": 123, "quantity": 2}])

    entries = change_log_module._instance.recent()
    assert len(entries) == 1
    assert entries[0]["service"] == "ozon"
    assert entries[0]["entity_type"] == "posting.ship"
    assert entries[0]["entity_id"] == "X-1"


def test_update_stocks_records_one_change_log_entry_per_item():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": []})

    client = _client_with_handler(handler)
    client.update_stocks([{"sku": "A1", "count": 5}, {"sku": "A2", "count": 0}], warehouse_id=999)

    entries = change_log_module._instance.recent()
    assert len(entries) == 2
    assert {e["entity_id"] for e in entries} == {"999:A1", "999:A2"}


def test_notification_list_returns_raw_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/notification/list"
        return httpx.Response(200, json={"urls": [{"url": "https://x/webhook", "enable": True, "availability_status": "GREEN"}], "total_count": 1})

    client = _client_with_handler(handler)
    result = client.notification_list()
    assert result["urls"][0]["availability_status"] == "GREEN"


def test_unprocessed_reviews_paginates_via_last_id():
    requests: list[httpx.Request] = []
    pages = [
        {"reviews": [{"id": "r1"}, {"id": "r2"}], "last_id": "cursor-2", "has_next": True},
        {"reviews": [{"id": "r3"}], "last_id": "", "has_next": False},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    client = _client_with_handler(handler)
    reviews = client.unprocessed_reviews()

    assert [r["id"] for r in reviews] == ["r1", "r2", "r3"]
    assert requests[0].url.path == "/v1/review/list"
    assert b'"status":"UNPROCESSED"' in requests[0].content.replace(b" ", b"")
    assert b'"last_id":"cursor-2"' in requests[1].content.replace(b" ", b"")


def test_unprocessed_reviews_clamps_limit_to_ozons_allowed_range():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"reviews": [], "last_id": "", "has_next": False})

    client = _client_with_handler(handler)
    client.unprocessed_reviews(limit=5)  # below OZON's allowed [20, 100]
    body = requests[0].content.replace(b" ", b"")
    assert b'"limit":20' in body

    client.unprocessed_reviews(limit=500)  # above the allowed range
    body = requests[1].content.replace(b" ", b"")
    assert b'"limit":100' in body


def test_product_names_by_sku_maps_via_nested_sources():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/product/info/list"
        return httpx.Response(200, json={"items": [
            {"offer_id": "LE148", "name": "Алмазная мозаика Коты", "sources": [{"sku": 1536499166}]},
            {"offer_id": "MC1148", "name": "Карты таро", "sources": [{"sku": 2142887836}]},
        ]})

    client = _client_with_handler(handler)
    names = client.product_names_by_sku([1536499166, 2142887836])

    assert names[1536499166] == {"offer_id": "LE148", "name": "Алмазная мозаика Коты"}
    assert names[2142887836] == {"offer_id": "MC1148", "name": "Карты таро"}


def test_product_names_by_sku_skips_request_for_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called for an empty sku list")

    client = _client_with_handler(handler)
    assert client.product_names_by_sku([]) == {}


def test_create_review_comment_posts_expected_body_and_returns_comment_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/review/comment/create"
        return httpx.Response(200, json={"comment_id": "comment-1"})

    client = _client_with_handler(handler)
    comment_id = client.create_review_comment(review_id="review-1", text="Спасибо за отзыв!")

    assert comment_id == "comment-1"
