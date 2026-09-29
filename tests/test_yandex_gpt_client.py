import json

import httpx
import pytest

from sync_service.yandex_gpt_client import YandexGPTClient


def _client_with_handler(handler):
    client = YandexGPTClient(api_key="test-key", folder_id="test-folder", model="yandexgpt/latest")
    original_headers = client._client._client.headers
    client._client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://llm.api.cloud.yandex.net", headers=original_headers)
    return client


def test_complete_sends_system_and_user_message_with_auth_header():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hello back"}}]})

    client = _client_with_handler(handler)
    result = client.complete(system="You are helpful.", user_message="Hi there")

    assert result == "hello back"
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Api-Key test-key"
    body = json.loads(requests[0].content)
    assert body["model"] == "gpt://test-folder/yandexgpt/latest"
    assert body["messages"] == [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Hi there"}]


def test_complete_returns_empty_string_for_no_choices():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = _client_with_handler(handler)
    assert client.complete(system="x", user_message="y") == ""


def test_complete_passes_max_tokens():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client_with_handler(handler)
    client.complete(system="x", user_message="y", max_tokens=200)
    assert json.loads(requests[0].content)["max_tokens"] == 200


def test_complete_strips_whitespace():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "  padded  \n"}}]})

    client = _client_with_handler(handler)
    assert client.complete(system="x", user_message="y") == "padded"


def test_complete_raises_on_image_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not make a request when rejecting image input")

    client = _client_with_handler(handler)
    with pytest.raises(NotImplementedError):
        client.complete(system="x", user_message="y", image_bytes=b"fake-jpeg")


def test_supports_images_is_false():
    assert YandexGPTClient.supports_images is False
