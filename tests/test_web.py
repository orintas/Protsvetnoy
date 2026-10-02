import io
import json

import sync_service.web as web_module
from sync_service.error_log import ErrorLog
from sync_service.planfix_log import PlanFixSyncLog
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
        "PATH_INFO": "/api/yandex-market/webhook/notification", "QUERY_STRING": "", "REQUEST_METHOD": "POST",
        "HTTP_X_FORWARDED_FOR": "5.45.207.10, 172.18.0.5",
        "REMOTE_ADDR": "172.18.0.5",
        "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
    }
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    application(environ, start_response)
    assert captured["status"] == "403 Forbidden"


def _planfix_form_environ(fields: dict[str, str]) -> dict:
    from urllib.parse import urlencode

    body = urlencode(fields).encode()
    return {
        "PATH_INFO": "/api/planfix/webhook", "QUERY_STRING": "", "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
    }


class _FakeClosableClient:
    def close(self):
        pass


def test_planfix_webhook_logs_success_and_returns_the_result(tmp_path, monkeypatch):
    monkeypatch.setattr(web_module, "PlanFixSyncLog", lambda: PlanFixSyncLog(str(tmp_path / "planfix.sqlite3")))
    monkeypatch.setattr(web_module, "PlanFixClient", lambda **kwargs: _FakeClosableClient())
    monkeypatch.setattr(web_module, "MoySkladClient", lambda **kwargs: _FakeClosableClient())
    monkeypatch.setattr(web_module, "sync_contact", lambda planfix, moysklad, *, contact_id, department_raw: {"MoySkladID": "ms-1", "MoySkladUrl": "https://example/ms-1"})
    environ = _planfix_form_environ({"ContactID": "42", "Отдел": "Varvikas"})
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    body = b"".join(application(environ, start_response))

    assert captured["status"] == "200 OK"
    assert json.loads(body) == {"MoySkladID": "ms-1", "MoySkladUrl": "https://example/ms-1"}
    entries = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3")).recent()
    assert len(entries) == 1
    assert entries[0]["kind"] == "contact_sync"
    assert entries[0]["status"] == "success"
    assert entries[0]["external_id"] == "42"


def test_planfix_webhook_rejects_missing_contact_id(tmp_path, monkeypatch):
    monkeypatch.setattr(web_module, "PlanFixSyncLog", lambda: PlanFixSyncLog(str(tmp_path / "planfix.sqlite3")))
    monkeypatch.setattr(web_module, "ErrorLog", lambda: ErrorLog(str(tmp_path / "errors.sqlite3")))
    environ = _planfix_form_environ({"Отдел": "Varvikas"})
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    application(environ, start_response)
    assert captured["status"] == "400 Bad Request"


def test_planfix_webhook_responds_400_for_a_non_company_contact(tmp_path, monkeypatch):
    monkeypatch.setattr(web_module, "PlanFixSyncLog", lambda: PlanFixSyncLog(str(tmp_path / "planfix.sqlite3")))
    monkeypatch.setattr(web_module, "PlanFixClient", lambda **kwargs: _FakeClosableClient())
    monkeypatch.setattr(web_module, "MoySkladClient", lambda **kwargs: _FakeClosableClient())

    def _raise_not_a_company(planfix, moysklad, *, contact_id, department_raw):
        from sync_service.planfix_contact_sync import NotACompany
        raise NotACompany()

    monkeypatch.setattr(web_module, "sync_contact", _raise_not_a_company)
    environ = _planfix_form_environ({"ContactID": "42", "Отдел": "Varvikas"})
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    application(environ, start_response)
    assert captured["status"] == "400 Bad Request"
    assert captured["headers"]["error"] == "Это не компания"
