from datetime import datetime

import httpx

from sync_service.moysklad import MoySkladClient
from sync_service.novicloud import NovicloudClient


def test_novicloud_products_uses_v2_and_barcode_filter():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": 200, "dane": []})

    client = NovicloudClient(
        base_url="https://system.novicloud.pl/rest/api",
        version="v2",
        account="Varvikas",
        password="secret",
    )
    client._client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://system.novicloud.pl/rest/api/v2/Varvikas",
    )
    client.products(barcode="123")
    assert requests[0].url.path.endswith("/towary")
    assert requests[0].url.params["kod"] == "123"


def test_novicloud_sales_builds_date_filter():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": 200, "dane": []})

    client = NovicloudClient(
        base_url="https://system.novicloud.pl/rest/api",
        version="v2",
        account="Varvikas",
        password="secret",
    )
    client._client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://system.novicloud.pl/rest/api/v2/Varvikas",
    )
    client.sales(date_from=datetime(2026, 9, 1, 0, 0, 0))
    assert requests[0].url.params["data"].startswith("min2026-09-01T00:00:00")


def test_moysklad_stock_report_path():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"rows": []})

    client = MoySkladClient(
        base_url="https://api.moysklad.ru/api/remap/1.2",
        token="secret",
    )
    client._client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.moysklad.ru/api/remap/1.2",
    )
    client.stock_report()
    assert requests[0].url.path.endswith("/report/stock/all")


def _moysklad_client_with_handler(handler):
    client = MoySkladClient(base_url="https://api.moysklad.ru/api/remap/1.2", token="secret")
    client._client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.moysklad.ru/api/remap/1.2")
    return client


def test_demand_template_from_customer_order_puts_order_meta():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"organization": {}, "positions": {"meta": {}}})

    client = _moysklad_client_with_handler(handler)
    client.demand_template_from_customer_order("order-1")

    assert requests[0].method == "PUT"
    assert requests[0].url.path.endswith("/entity/demand/new")
    body = requests[0].content
    assert b'"customerOrder"' in body
    assert b"entity/customerorder/order-1" in body


def test_create_demand_posts_template():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "demand-1", "name": "00001"})

    client = _moysklad_client_with_handler(handler)
    result = client.create_demand({"organization": {}})

    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/entity/demand")
    assert result["id"] == "demand-1"


def test_demand_positions_lists_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/entity/demand/demand-1/positions")
        return httpx.Response(200, json={"rows": [{"id": "pos-1", "quantity": 2}]})

    client = _moysklad_client_with_handler(handler)
    positions = client.demand_positions("demand-1")

    assert positions == [{"id": "pos-1", "quantity": 2}]


def test_stock_by_slot_builds_filter_with_assortments_and_store():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"assortmentId": "a1", "storeId": "s1", "slotId": "slot-1", "stock": 5}])

    client = _moysklad_client_with_handler(handler)
    rows = client.stock_by_slot(["a1", "a2"], store_id="s1")

    assert requests[0].url.path.endswith("/report/stock/byslot/current")
    filter_value = requests[0].url.params["filter"]
    assert "assortmentId=a1" in filter_value
    assert "assortmentId=a2" in filter_value
    assert "storeId=s1" in filter_value
    assert rows[0]["slotId"] == "slot-1"


def test_counterparty_by_email_builds_filter():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["filter"] == "email=a@b.com"
        return httpx.Response(200, json={"rows": [{"id": "cp-1"}]})

    client = _moysklad_client_with_handler(handler)
    found = client.counterparty_by_email("a@b.com")

    assert found == {"id": "cp-1"}


def test_counterparty_by_email_returns_none_when_empty():
    client = _moysklad_client_with_handler(lambda r: httpx.Response(200, json={"rows": []}))
    assert client.counterparty_by_email("nobody@example.com") is None


def test_counterparty_by_email_skips_request_for_empty_email():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not make a request for an empty email")

    client = _moysklad_client_with_handler(handler)
    assert client.counterparty_by_email("") is None


def test_create_counterparty_posts_expected_body():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "cp-1", "meta": {"uuidHref": "https://x/cp-1"}})

    client = _moysklad_client_with_handler(handler)
    client.create_counterparty(name="ООО Ромашка", email="a@b.com", phone="+70000000000", group_id="group-1")

    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/entity/counterparty")
    body = requests[0].content.decode("utf-8")
    assert "ООО Ромашка" in body
    assert "entity/group/group-1" in body


def test_update_counterparty_puts_to_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path.endswith("/entity/counterparty/cp-1")
        return httpx.Response(200, json={"id": "cp-1"})

    client = _moysklad_client_with_handler(handler)
    client.update_counterparty("cp-1", name="x", email="x@x.com", phone="", group_id="g")


def test_counterparty_report_returns_none_on_404():
    client = _moysklad_client_with_handler(lambda r: httpx.Response(404, json={"errors": []}))
    assert client.counterparty_report("missing-id") is None


def test_counterparty_report_returns_stats():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/report/counterparty/cp-1")
        return httpx.Response(200, json={"demandsCount": 5})

    client = _moysklad_client_with_handler(handler)
    assert client.counterparty_report("cp-1") == {"demandsCount": 5}


def test_stock_by_products_builds_filter_with_products_and_store():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"rows": [{"code": "MG2466", "stock": 1, "reserve": 1, "quantity": 0}]})

    client = _moysklad_client_with_handler(handler)
    rows = client.stock_by_products(["p1", "p2"], store_id="s1")

    assert requests[0].url.path.endswith("/report/stock/all")
    filter_value = requests[0].url.params["filter"]
    assert "product=" in filter_value and "p1" in filter_value
    assert "p2" in filter_value
    assert "store=" in filter_value and "s1" in filter_value
    assert rows[0]["code"] == "MG2466"


def test_customer_order_positions_lists_rows_with_assortment_expanded():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/entity/customerorder/order-1/positions")
        assert request.url.params["expand"] == "assortment"
        return httpx.Response(200, json={"rows": [{"id": "pos-1", "assortment": {"code": "MG2466"}}]})

    client = _moysklad_client_with_handler(handler)
    positions = client.customer_order_positions("order-1")

    assert positions == [{"id": "pos-1", "assortment": {"code": "MG2466"}}]


def test_create_customer_order_includes_project_when_given():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"name": "00042"})

    client = _moysklad_client_with_handler(handler)
    client.create_customer_order(
        moment="2026-10-06 00:00:00", organization_id="org-1", agent_id="agent-1", store_id="store-1",
        external_code="ext-1", positions=[], project_id="project-1",
    )

    body = requests[0].content.decode("utf-8")
    assert "entity/project/project-1" in body


def test_create_customer_order_omits_project_when_not_given():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"name": "00042"})

    client = _moysklad_client_with_handler(handler)
    client.create_customer_order(moment="2026-10-06 00:00:00", organization_id="org-1", agent_id="agent-1", store_id="store-1", external_code="ext-1", positions=[])

    body = requests[0].content.decode("utf-8")
    assert "entity/project" not in body


def test_create_loss_posts_expected_body():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "loss-1", "name": "8д900", "meta": {"uuidHref": "https://x/loss-1"}})

    client = _moysklad_client_with_handler(handler)
    positions = [{"quantity": 1.0, "assortment": {"meta": {"href": "https://x/entity/product/p1"}}}]
    result = client.create_loss(organization_id="org-1", store_id="store-1", positions=positions, description="тест")

    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/entity/loss")
    body = requests[0].content.decode("utf-8")
    assert "entity/organization/org-1" in body
    assert "entity/store/store-1" in body
    assert "тест" in body
    assert result["name"] == "8д900"


def test_set_position_slot_puts_slot_meta():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "pos-1"})

    client = _moysklad_client_with_handler(handler)
    client.set_position_slot("demand-1", "pos-1", store_id="store-1", slot_id="slot-1")

    assert requests[0].method == "PUT"
    assert requests[0].url.path.endswith("/entity/demand/demand-1/positions/pos-1")
    body = requests[0].content
    assert b"entity/store/store-1/slots/slot-1" in body
    assert b'"type":"slot"' in body
