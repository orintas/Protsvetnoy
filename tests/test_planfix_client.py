import httpx

from sync_service.planfix_client import PlanFixClient


def _client_with_handler(handler):
    client = PlanFixClient(base_url="https://x.planfix.ru/rest", api_key="secret")
    client._client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x.planfix.ru/rest")
    return client


def test_get_contact_requests_system_and_extra_fields():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": "success", "contact": {"id": 1, "isCompany": True}})

    client = _client_with_handler(handler)
    contact = client.get_contact("1", extra_field_ids=["99"])

    assert requests[0].url.path.endswith("/contact/1")
    fields = requests[0].url.params["fields"]
    assert "isCompany" in fields
    assert "99" in fields
    assert contact == {"id": 1, "isCompany": True}


def test_contact_custom_field_id_resolves_by_name_and_caches():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"customFields": [{"id": 99, "name": "MoySkladID"}, {"id": 5, "name": "Отдел"}]})

    client = _client_with_handler(handler)

    assert client.contact_custom_field_id("MoySkladID") == "99"
    assert client.contact_custom_field_id("Отдел") == "5"
    assert client.contact_custom_field_id("Unknown") is None
    assert len(requests) == 1  # cached after the first call
