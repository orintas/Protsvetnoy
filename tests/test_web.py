import io
import json

from sync_service.web import _client_ip, application


def test_client_ip_prefers_last_forwarded_for_entry():
    # Caddy appends the peer it actually saw; that's the trustworthy entry.
    assert _client_ip({"HTTP_X_FORWARDED_FOR": "1.2.3.4, 5.45.207.10", "REMOTE_ADDR": "172.18.0.5"}) == "5.45.207.10"


def test_client_ip_falls_back_to_remote_addr_without_header():
    assert _client_ip({"REMOTE_ADDR": "5.45.207.10"}) == "5.45.207.10"


def test_webhook_rejects_spoofed_first_forwarded_for_entry():
    # A client claiming to be an allowed IP as the *first* hop must not fool
    # the check — only the last (Caddy-appended) entry is trusted.
    body = json.dumps({"notificationType": "PING"}).encode()
    environ = {
        "PATH_INFO": "/api/yandex-market/webhook", "QUERY_STRING": "", "REQUEST_METHOD": "POST",
        "HTTP_X_FORWARDED_FOR": "5.45.207.10, 172.18.0.5",
        "REMOTE_ADDR": "172.18.0.5",
        "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
    }
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    application(environ, start_response)
    assert captured["status"] == "403 Forbidden"
