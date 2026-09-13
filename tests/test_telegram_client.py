import httpx
import pytest

from sync_service.telegram_client import TelegramClient


def _client_with_handler(handler):
    client = TelegramClient(bot_token="test-token")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org/bottest-token")
    return client


def test_send_document_posts_multipart_with_caption():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _client_with_handler(handler)
    client.send_document(chat_id="-100123", document=b"%PDF-fake", filename="42.pdf", caption="hello")
    assert requests[0].url.path == "/bottest-token/sendDocument"
    body = requests[0].content
    assert b"-100123" in body
    assert b"42.pdf" in body
    assert b"%PDF-fake" in body
    assert b"hello" in body


def test_send_document_raises_on_ok_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    client = _client_with_handler(handler)
    with pytest.raises(RuntimeError, match="chat not found"):
        client.send_document(chat_id="-100123", document=b"%PDF-fake", filename="42.pdf")


def test_send_document_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    client = _client_with_handler(handler)
    with pytest.raises(RuntimeError, match="400"):
        client.send_document(chat_id="-100123", document=b"%PDF-fake", filename="42.pdf")
