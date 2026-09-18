import httpx
import pytest

import sync_service.change_log as change_log_module
from sync_service.telegram_client import TelegramClient


def _client_with_handler(handler):
    client = TelegramClient(bot_token="test-token")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org/bottest-token")
    return client


def test_proxy_is_passed_through_to_the_http_client():
    client = TelegramClient(bot_token="test-token", proxy="socks5://telegram-proxy:1080")
    assert len(client._client._mounts) == 1


def test_no_proxy_means_direct_connection():
    client = TelegramClient(bot_token="test-token")
    assert client._client._mounts == {}
    client = TelegramClient(bot_token="test-token", proxy="")
    assert client._client._mounts == {}


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

    entries = change_log_module._instance.recent()
    assert len(entries) == 1
    assert entries[0]["service"] == "telegram"
    assert entries[0]["action"] == "send"
    assert entries[0]["entity_id"] == "-100123"


def test_send_document_raises_on_ok_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    client = _client_with_handler(handler)
    with pytest.raises(RuntimeError, match="chat not found"):
        client.send_document(chat_id="-100123", document=b"%PDF-fake", filename="42.pdf")
    assert change_log_module._instance.recent() == []  # failed send is not logged as a change


def test_send_document_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    client = _client_with_handler(handler)
    with pytest.raises(RuntimeError, match="400"):
        client.send_document(chat_id="-100123", document=b"%PDF-fake", filename="42.pdf")


def test_send_document_retries_on_transport_error_then_succeeds(monkeypatch):
    import sync_service.telegram_client as mod
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise httpx.ConnectError("Connection reset by peer", request=request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _client_with_handler(handler)
    client.send_document(chat_id="-100123", document=b"%PDF-fake", filename="42.pdf")
    assert attempts["count"] == 3


def test_send_document_raises_after_exhausting_retries_on_transport_error(monkeypatch):
    import sync_service.telegram_client as mod
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection reset by peer", request=request)

    client = _client_with_handler(handler)
    with pytest.raises(httpx.ConnectError):
        client.send_document(chat_id="-100123", document=b"%PDF-fake", filename="42.pdf")
