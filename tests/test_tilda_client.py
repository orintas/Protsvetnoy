import httpx
import pytest

import sync_service.tilda_client as tilda_client_module
from sync_service.tilda_client import TildaCommerceMLClient, TildaExchangeError


def _client_with_handler(handler, monkeypatch):
    monkeypatch.setattr(tilda_client_module, "IMPORT_STEP_PAUSE_SECONDS", 0)
    client = TildaCommerceMLClient(login="208015", password="secret")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), auth=("208015", "secret"))
    return client


def test_exchange_runs_the_1c_protocol_and_sends_the_session_cookie(monkeypatch):
    requests: list[httpx.Request] = []
    import_calls = {"offers.xml": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        mode = request.url.params["mode"]
        if mode == "checkauth":
            return httpx.Response(200, text="success\nPHPSESSID\nabc123\n")
        if mode == "init":
            return httpx.Response(200, text="zip=no\nfile_limit=10\n")
        if mode == "file":
            return httpx.Response(200, text="success")
        if request.url.params["filename"] == "offers.xml" and import_calls["offers.xml"] == 0:
            import_calls["offers.xml"] += 1
            return httpx.Response(200, text="progress")
        return httpx.Response(200, text="success")

    client = _client_with_handler(handler, monkeypatch)
    client.exchange([("import.xml", b"0123456789abcde"), ("offers.xml", b"short")])

    steps = [(r.method, r.url.params["mode"], r.url.params.get("filename")) for r in requests]
    assert steps == [
        ("GET", "checkauth", None), ("GET", "init", None),
        ("POST", "file", "import.xml"), ("POST", "file", "import.xml"),  # 15 bytes, file_limit=10 -> two pieces
        ("GET", "import", "import.xml"),
        ("POST", "file", "offers.xml"), ("GET", "import", "offers.xml"), ("GET", "import", "offers.xml"),
    ]
    assert requests[2].content == b"0123456789" and requests[3].content == b"abcde"
    assert "PHPSESSID=abc123" in requests[1].headers["cookie"]
    assert requests[0].headers["authorization"].startswith("Basic ")


def test_failed_import_raises_with_tildas_answer(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        mode = request.url.params["mode"]
        if mode == "checkauth":
            return httpx.Response(200, text="success\ncookie\nvalue")
        if mode == "import":
            return httpx.Response(200, text="failure\nТовар не найден")
        return httpx.Response(200, text="success")

    client = _client_with_handler(handler, monkeypatch)
    with pytest.raises(TildaExchangeError, match="Товар не найден"):
        client.exchange([("import.xml", b"<x/>")])


def test_checkauth_failure_is_an_error(monkeypatch):
    client = _client_with_handler(lambda request: httpx.Response(200, text="failure\nwrong password"), monkeypatch)
    with pytest.raises(TildaExchangeError, match="wrong password"):
        client.checkauth()
