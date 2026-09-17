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


def test_post_bytes_returns_raw_response_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b'{"posting_number":["X-1"]}'
        return httpx.Response(200, content=b"%PDF-fake", headers={"content-type": "application/pdf"})

    client = JsonClient(base_url="https://example.test")
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))

    assert client.post_bytes("/label", {"posting_number": ["X-1"]}) == b"%PDF-fake"


def test_post_bytes_raises_apierror_on_error_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="not ready")

    client = JsonClient(base_url="https://example.test")
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))

    raised = False
    try:
        client.post_bytes("/label", {})
    except ApiError:
        raised = True
    assert raised


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


def test_get_retries_on_transport_error_then_succeeds():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(200, json={"status": 200, "dane": []})

    client = JsonClient(base_url="https://example.test", max_retries=3, retry_backoff=0.01)
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))

    payload = client.get("/towary")
    assert payload == {"status": 200, "dane": []}
    assert attempts["count"] == 3


def test_get_raises_after_exhausting_retries_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    client = JsonClient(base_url="https://example.test", max_retries=2, retry_backoff=0.01)
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))

    raised = False
    try:
        client.get("/towary")
    except httpx.ReadTimeout:
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
