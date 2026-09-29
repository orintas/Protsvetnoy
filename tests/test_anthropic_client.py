import json

import httpx

from sync_service.anthropic_client import AnthropicClient


def _client_with_handler(handler):
    client = AnthropicClient(api_key="test-key", model="claude-sonnet-5")
    original_headers = client._client._client.headers
    client._client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com", headers=original_headers)
    return client


def test_complete_sends_system_and_user_message_with_auth_headers():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hello back"}]})

    client = _client_with_handler(handler)
    result = client.complete(system="You are helpful.", user_message="Hi there")

    assert result == "hello back"
    assert requests[0].url.path == "/v1/messages"
    assert requests[0].headers["x-api-key"] == "test-key"
    assert requests[0].headers["anthropic-version"] == "2023-06-01"
    body = json.loads(requests[0].content)
    assert body["model"] == "claude-sonnet-5"
    assert body["system"] == "You are helpful."
    assert body["messages"] == [{"role": "user", "content": "Hi there"}]


def test_complete_joins_multiple_text_blocks():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Part one. "}, {"type": "text", "text": "Part two."}]})

    client = _client_with_handler(handler)
    assert client.complete(system="x", user_message="y") == "Part one. Part two."


def test_complete_ignores_non_text_blocks():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "1"}, {"type": "text", "text": "answer"}]})

    client = _client_with_handler(handler)
    assert client.complete(system="x", user_message="y") == "answer"


def test_complete_returns_empty_string_for_no_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": []})

    client = _client_with_handler(handler)
    assert client.complete(system="x", user_message="y") == ""


def test_complete_passes_max_tokens():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _client_with_handler(handler)
    client.complete(system="x", user_message="y", max_tokens=200)
    assert json.loads(requests[0].content)["max_tokens"] == 200


def test_complete_sends_image_as_base64_content_block():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "62260935105"}]})

    client = _client_with_handler(handler)
    result = client.complete(system="x", user_message="какой номер?", image_bytes=b"fake-jpeg-bytes", image_media_type="image/png")

    assert result == "62260935105"
    body = json.loads(requests[0].content)
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1] == {"type": "text", "text": "какой номер?"}

    import base64
    assert base64.b64decode(content[0]["source"]["data"]) == b"fake-jpeg-bytes"


def test_complete_without_image_sends_plain_string_content():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _client_with_handler(handler)
    client.complete(system="x", user_message="hello")
    body = json.loads(requests[0].content)
    assert body["messages"][0]["content"] == "hello"
