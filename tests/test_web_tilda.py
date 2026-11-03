import gzip
import io
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import sync_service.web as web_module
from sync_service.tilda_order_sync import TildaOrderQueue
from sync_service.web import application


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Every data/ path is relative, so running in tmp_path isolates them."""
    monkeypatch.chdir(tmp_path)
    settings = SimpleNamespace(tilda_webhook_secret="s3cret")
    monkeypatch.setattr(web_module.Settings, "from_env", classmethod(lambda cls: settings))
    started = []
    monkeypatch.setattr(web_module, "process_queued", lambda *args: started.append(args[0]))
    return SimpleNamespace(path=tmp_path, settings=settings, started=started)


def _call(environ):
    captured = {}

    def start_response(status, headers):
        captured["status"], captured["headers"] = status, dict(headers)

    body = b"".join(application({"QUERY_STRING": "", **environ}, start_response))
    return captured["status"], captured["headers"], body


def _post(fields, headers=None):
    body = urlencode(fields).encode()
    return _call({"PATH_INFO": "/api/tilda/webhook/orders", "REQUEST_METHOD": "POST", "CONTENT_TYPE": "application/x-www-form-urlencoded",
                  "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), **(headers or {})})


def test_stock_json_is_served_gzipped_with_cors(site):
    (site.path / "data").mkdir()
    (site.path / "data" / "site_stock.json").write_text(json.dumps({"updated": "x", "items": {"RB003": {"main": 2}}}))
    status, headers, body = _call({"PATH_INFO": "/api/site/stock.json", "REQUEST_METHOD": "GET",
                                   "HTTP_ORIGIN": "https://evil.example", "HTTP_ACCEPT_ENCODING": "gzip, br"})
    assert status == "200 OK"
    assert headers["Access-Control-Allow-Origin"] == "https://protsvetnoy.com"
    assert headers["Cache-Control"] == "public, max-age=60"
    assert headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(body))["items"] == {"RB003": {"main": 2}}


def test_stock_json_is_unavailable_before_the_first_snapshot(site):
    status, _, _ = _call({"PATH_INFO": "/api/site/stock.json", "REQUEST_METHOD": "GET"})
    assert status == "503 Service Unavailable"


def test_tilda_connection_test_is_answered_ok_without_a_key(site):
    status, _, body = _post([("test", "test")])
    assert (status, body) == ("200 OK", b"ok")
    assert site.started == []


def test_order_with_a_wrong_key_is_rejected(site):
    status, _, _ = _post([("payment[orderid]", "T-1"), ("api_key", "wrong")])
    assert status == "401 Unauthorized"
    assert site.started == []


def test_paid_order_is_queued_once_and_processed_in_the_background(site):
    fields = [("Phone", "+79991234567"), ("payment[orderid]", "T-1"), ("payment[products][0][sku]", "RB003")]
    status, _, body = _post(fields, {"HTTP_X_API_KEY": "s3cret"})
    assert (status, body) == ("200 OK", b"ok")
    _post(fields, {"HTTP_X_API_KEY": "s3cret"})  # Tilda resends

    assert site.started == ["T-1"]
    item = TildaOrderQueue().claim("T-1")
    assert item["payload"]["payment"]["products"] == [{"sku": "RB003"}]
