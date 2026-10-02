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

    assert requests[0].url.path.endswith("/contact/contact:1")
    fields = requests[0].url.params["fields"]
    assert "isCompany" in fields
    assert "99" in fields
    assert contact == {"id": 1, "isCompany": True}


def test_get_contact_does_not_double_prefix_an_already_prefixed_id():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": "success", "contact": {"id": 1}})

    client = _client_with_handler(handler)
    client.get_contact("contact:1")

    assert requests[0].url.path.endswith("/contact/contact:1")


def test_contact_custom_field_id_resolves_by_name_and_caches():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # Real PlanFix response key is lowercase "customfields" — confirmed
        # live 2026-10-02 against the actual account. A previous version of
        # this test used "customFields" (camelCase), matching a bug in the
        # implementation rather than the real API, so it passed while the
        # live code silently resolved every field id to None.
        return httpx.Response(200, json={"result": "success", "customfields": [{"id": 99, "name": "MoySkladID"}, {"id": 5, "name": "Отдел"}]})

    client = _client_with_handler(handler)

    assert client.contact_custom_field_id("MoySkladID") == "99"
    assert client.contact_custom_field_id("Отдел") == "5"
    assert client.contact_custom_field_id("Unknown") is None
    assert len(requests) == 1  # cached after the first call
    assert requests[0].url.params["fields"] == "id,name"


def test_contact_custom_field_id_ignores_entries_missing_a_name():
    """Without fields=id,name, PlanFix returns bare {"id": N} entries with
    no "name" at all — confirmed live 2026-10-02, the second bug behind the
    MoySkladID lookup always resolving to None even after the lowercase-key
    fix. Entries missing "name" must be skipped, not raise a KeyError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "success", "customfields": [{"id": 75712}, {"id": 99, "name": "MoySkladID"}]})

    client = _client_with_handler(handler)

    assert client.contact_custom_field_id("MoySkladID") == "99"


def test_contact_custom_field_id_returns_none_for_camelcase_key():
    """Locks in the lowercase "customfields" key: a response using the
    wrong case for the key must not silently resolve anything."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "success", "customFields": [{"id": 99, "name": "MoySkladID"}]})

    client = _client_with_handler(handler)

    assert client.contact_custom_field_id("MoySkladID") is None
