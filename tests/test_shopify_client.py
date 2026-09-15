import httpx

from sync_service.shopify_client import ShopifyClient


def _client_with_handler(handler):
    client = ShopifyClient(shop_domain="varvikas.myshopify.com", access_token="test-token", api_version="2026-07")
    client._client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://varvikas.myshopify.com/admin/api/2026-07")
    return client


def test_find_variant_by_sku_parses_numeric_ids_from_gids():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/api/2026-07/graphql.json"
        return httpx.Response(200, json={
            "data": {"productVariants": {"edges": [{"node": {
                "id": "gid://shopify/ProductVariant/111",
                "sku": "ABC",
                "inventoryItem": {"id": "gid://shopify/InventoryItem/222"},
                "product": {"id": "gid://shopify/Product/333", "title": "Test"},
            }}]}}
        })

    client = _client_with_handler(handler)
    result = client.find_variant_by_sku("ABC")
    assert result == {"variant_id": 111, "inventory_item_id": 222, "product_id": 333}


def test_find_variant_by_sku_returns_none_when_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"productVariants": {"edges": []}}})

    client = _client_with_handler(handler)
    assert client.find_variant_by_sku("MISSING") is None


def test_create_product_posts_variant_and_base64_image():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"product": {"id": 1, "variants": [{"id": 2, "inventory_item_id": 3}]}})

    client = _client_with_handler(handler)
    result = client.create_product(title="ABC - Test", sku="ABC", price=12.5, vendor="TM Varvikas", product_type="Accessories", body_html="desc", image_bytes=b"fakejpeg")
    assert requests[0].url.path == "/admin/api/2026-07/products.json"
    body = requests[0].content.decode()
    assert '"sku":"ABC"' in body.replace(" ", "")
    assert '"price":"12.50"' in body.replace(" ", "")
    assert '"inventory_management":"shopify"' in body.replace(" ", "")
    assert "attachment" in body
    assert result == {"id": 1, "variants": [{"id": 2, "inventory_item_id": 3}]}


def test_update_product_puts_to_product_id_path():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"product": {"id": 1}})

    client = _client_with_handler(handler)
    client.update_product(1, 2, title="ABC - Test", sku="ABC", price=12.5, vendor="TM Varvikas", product_type="Accessories")
    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/admin/api/2026-07/products/1.json"
    assert '"inventory_management":"shopify"' in requests[0].content.decode().replace(" ", "")


def test_set_inventory_level_posts_expected_body():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"inventory_level": {}})

    client = _client_with_handler(handler)
    client.set_inventory_level(inventory_item_id=222, location_id=999, available=7)
    body = requests[0].content.replace(b" ", b"")
    assert b'"inventory_item_id":222' in body
    assert b'"location_id":999' in body
    assert b'"available":7' in body


def test_locations_returns_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"locations": [{"id": 66945810655, "name": "Varvikas Main Warehouse"}]})

    client = _client_with_handler(handler)
    assert client.locations() == [{"id": 66945810655, "name": "Varvikas Main Warehouse"}]
