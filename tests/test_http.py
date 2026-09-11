import httpx

from sync_service.http import ApiError, JsonClient


def test_get_retries_on_transient_server_error_then_succeeds():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, json={"status": 200, "dane": []})

    client = JsonClient(
        base_url="https://example.test",
        max_retries=3,
        retry_backoff=0.01,
    )
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))

    payload = client.get("/towary")
    assert payload == {"status": 200, "dane": []}
    assert attempts["count"] == 3


def test_get_raises_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    client = JsonClient(base_url="https://example.test", max_retries=2, retry_backoff=0.01)
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))

    try:
        client.get("/towary")
        raised = False
    except ApiError:
        raised = True
    assert raised


def test_get_does_not_retry_on_client_error():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(404, text="Not Found")

    client = JsonClient(base_url="https://example.test", max_retries=3, retry_backoff=0.01)
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))

    try:
        client.get("/towary")
    except ApiError:
        pass
    assert attempts["count"] == 1
