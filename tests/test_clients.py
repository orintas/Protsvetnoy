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
