import io
import json
from time import monotonic

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


def _planfix_new_goods_environ(fields: dict[str, str]) -> dict:
    from urllib.parse import urlencode

    body = urlencode(fields).encode()
    return {
        "PATH_INFO": "/api/planfix/webhook/new-goods", "QUERY_STRING": "", "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
    }


def test_planfix_new_goods_webhook_calls_launch_and_returns_created_ids(monkeypatch):
    monkeypatch.setattr(web_module, "PlanFixClient", lambda **kwargs: _FakeClosableClient())
    captured_call = {}

    def _fake_launch(planfix, *, task_id, log):
        captured_call["task_id"] = task_id
        return [555, 556]

    monkeypatch.setattr(web_module, "launch_new_product_tasks", _fake_launch)
    environ = _planfix_new_goods_environ({"task_id": "7230"})
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    body = b"".join(application(environ, start_response))

    assert captured["status"] == "200 OK"
    assert json.loads(body) == {"created": [555, 556]}
    assert captured_call["task_id"] == 7230


def test_planfix_new_goods_webhook_rejects_missing_task_id():
    environ = _planfix_new_goods_environ({})
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    application(environ, start_response)
    assert captured["status"] == "400 Bad Request"


def test_planfix_new_goods_webhook_rejects_a_non_numeric_task_id():
    environ = _planfix_new_goods_environ({"task_id": "not-a-number"})
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    application(environ, start_response)
    assert captured["status"] == "400 Bad Request"


def test_planfix_new_goods_webhook_returns_500_when_launch_raises(monkeypatch):
    monkeypatch.setattr(web_module, "PlanFixClient", lambda **kwargs: _FakeClosableClient())

    def _raise(planfix, *, task_id, log):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_module, "launch_new_product_tasks", _raise)
    environ = _planfix_new_goods_environ({"task_id": "7230"})
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    application(environ, start_response)
    assert captured["status"] == "500 Internal Server Error"


def _sample_moysklad_product(code: str = "ABC1", name: str = "Test product") -> dict:
    return {
        "id": "prod-1",
        "code": code,
        "name": name,
        "pathName": "Painting by numbers",
        "updated": "2026-01-01 00:00:00",
        "salePrices": [{"priceType": {"name": "Cena w Polsce"}, "value": 1000}],
    }


class _FakeCategorySyncConfig:
    def __init__(self, *args, **kwargs):
        pass

    def load(self):
        return {"novicloud": ["Painting by numbers"], "shopify": []}


class _ExplodingMoySkladClient:
    """Fails the test if /generate refetches instead of using the cache."""

    def __init__(self, **kwargs):
        pass

    def products(self):
        raise AssertionError("MoySkladClient.products() should not be called when the catalog cache is fresh")

    def close(self):
        pass


class _ExplodingNovicloudClient:
    def __init__(self, **kwargs):
        pass

    def all_products(self):
        raise AssertionError("NovicloudClient.all_products() should not be called when the catalog cache is fresh")

    def close(self):
        pass


class _FakeMoySkladClient:
    def __init__(self, products, **kwargs):
        self._products = products
        self.closed = False

    def products(self):
        return self._products

    def products_pages(self):
        yield self._products

    def close(self):
        self.closed = True


class _FakeNovicloudClient:
    def __init__(self, products, **kwargs):
        self._products = products
        self.closed = False

    def all_products(self):
        return self._products

    def all_products_pages(self):
        yield self._products

    def close(self):
        self.closed = True


def _reset_catalog_cache():
    web_module._catalog_cache["at"] = 0.0
    web_module._catalog_cache["moysklad"] = None
    web_module._catalog_cache["novicloud"] = None


def _generate_environ(codes: list[str], format_name: str = "csv") -> dict:
    from urllib.parse import urlencode

    query = urlencode({"format": format_name, "codes": ",".join(codes)})
    return {"PATH_INFO": "/generate", "QUERY_STRING": query, "REQUEST_METHOD": "GET"}


def test_generate_reuses_cached_catalog_without_refetching(monkeypatch):
    # /generate used to repeat the same full MoySklad+Novicloud fetch
    # /api/compare-stream had just finished seconds earlier, which on the
    # real catalog took 30-80s and made the "Скачать CSV" button look hung.
    _reset_catalog_cache()
    monkeypatch.setattr(web_module, "CategorySyncConfig", _FakeCategorySyncConfig)
    monkeypatch.setattr(web_module, "MoySkladClient", _ExplodingMoySkladClient)
    monkeypatch.setattr(web_module, "NovicloudClient", _ExplodingNovicloudClient)
    product = _sample_moysklad_product()
    web_module._store_catalog_cache([product], [])

    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    body = b"".join(application(_generate_environ(["ABC1"]), start_response))

    assert captured["status"] == "200 OK"
    assert b"ABC1" in body


def test_generate_rejects_without_refetching_when_cache_is_empty(monkeypatch):
    # The download button is only ever enabled right after a successful
    # "Сравнить каталоги" run, so a missing cache means that comparison is
    # stale/never happened — /generate must not silently repeat the slow
    # full-catalog fetch (that repeat fetch is the original bug).
    _reset_catalog_cache()
    monkeypatch.setattr(web_module, "CategorySyncConfig", _FakeCategorySyncConfig)
    monkeypatch.setattr(web_module, "MoySkladClient", _ExplodingMoySkladClient)
    monkeypatch.setattr(web_module, "NovicloudClient", _ExplodingNovicloudClient)

    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    body = b"".join(application(_generate_environ(["ABC1"]), start_response))

    assert captured["status"] == "409 Conflict"
    assert "Сравнить каталоги" in body.decode("utf-8")


def test_generate_rejects_without_refetching_when_cache_is_stale(monkeypatch):
    _reset_catalog_cache()
    monkeypatch.setattr(web_module, "CategorySyncConfig", _FakeCategorySyncConfig)
    monkeypatch.setattr(web_module, "MoySkladClient", _ExplodingMoySkladClient)
    monkeypatch.setattr(web_module, "NovicloudClient", _ExplodingNovicloudClient)
    stale_product = _sample_moysklad_product(code="STALE1")
    web_module._store_catalog_cache([stale_product], [])
    web_module._catalog_cache["at"] = monotonic() - web_module._CATALOG_CACHE_TTL_SECONDS - 1

    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    body = b"".join(application(_generate_environ(["STALE1"]), start_response))

    assert captured["status"] == "409 Conflict"
    assert "Сравнить каталоги" in body.decode("utf-8")


def test_compare_stream_populates_catalog_cache(monkeypatch):
    _reset_catalog_cache()
    monkeypatch.setattr(web_module, "CategorySyncConfig", _FakeCategorySyncConfig)
    product = _sample_moysklad_product()
    monkeypatch.setattr(web_module, "MoySkladClient", lambda **kwargs: _FakeMoySkladClient([product], **kwargs))
    monkeypatch.setattr(web_module, "NovicloudClient", lambda **kwargs: _FakeNovicloudClient([], **kwargs))

    environ = {"PATH_INFO": "/api/compare-stream", "QUERY_STRING": "", "REQUEST_METHOD": "GET"}
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    list(application(environ, start_response))

    assert captured["status"] == "200 OK"
    cached = web_module._cached_catalogs()
    assert cached is not None
    cached_moysklad, cached_novicloud = cached
    assert cached_moysklad == [product]
    assert cached_novicloud == []


class _PagedMoySkladClient:
    def __init__(self, pages, **kwargs):
        self._pages = pages

    def products_pages(self):
        yield from self._pages

    def close(self):
        pass


class _PagedNovicloudClient:
    def __init__(self, pages, **kwargs):
        self._pages = pages

    def all_products_pages(self):
        yield from self._pages

    def close(self):
        pass


def test_compare_stream_reports_a_running_count_as_pages_arrive(monkeypatch):
    # A single static "Загружаем каталог…" label made a slow (~30-80s)
    # catalog fetch look stuck. Progress events with a running item count
    # per page let the UI show it's actually moving.
    _reset_catalog_cache()
    monkeypatch.setattr(web_module, "CategorySyncConfig", _FakeCategorySyncConfig)
    moysklad_pages = [[_sample_moysklad_product(code="A1")], [_sample_moysklad_product(code="A2")]]
    novicloud_pages = [[{"kod": "A1"}], [{"kod": "A2"}], [{"kod": "A3"}]]
    monkeypatch.setattr(web_module, "MoySkladClient", lambda **kwargs: _PagedMoySkladClient(moysklad_pages, **kwargs))
    monkeypatch.setattr(web_module, "NovicloudClient", lambda **kwargs: _PagedNovicloudClient(novicloud_pages, **kwargs))

    events = [json.loads(line) for line in web_module._compare_stream()]

    moysklad_counts = [e["count"] for e in events if e["stage"] == "moysklad"]
    novicloud_counts = [e["count"] for e in events if e["stage"] == "novicloud"]
    assert moysklad_counts == [0, 1, 2]
    assert novicloud_counts == [0, 1, 2, 3]
    assert events[-1]["stage"] == "done"


def _get(path):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(application({"PATH_INFO": path, "QUERY_STRING": "", "REQUEST_METHOD": "GET"}, start_response))
    return captured, body



class _FakeLabelYandex:
    calls = []

    def __init__(self, **kwargs):
        pass

    def get_order_label(self, order_id, *, campaign_id):
        self.calls.append((order_id, campaign_id))
        return b"%PDF-fake"

    def close(self):
        pass


def _label_route(monkeypatch):
    from sync_service.yandex_market_order_sync import label_link_url

    monkeypatch.setenv("YANDEX_MARKET_API_KEY", "key")
    monkeypatch.setattr(web_module, "LABEL_LINK_CAMPAIGNS", {"555"})
    monkeypatch.setattr(web_module, "YandexMarketClient", _FakeLabelYandex)
    _FakeLabelYandex.calls = []
    return label_link_url(555, 999, "key").removeprefix("https://protsvetnoy.us")


def test_label_route_fetches_the_label_from_market_for_a_signed_link(monkeypatch):
    path = _label_route(monkeypatch)

    captured, body = _get(path)

    assert captured["status"] == "200 OK"
    assert captured["headers"]["Content-Type"] == "application/pdf"
    assert body == b"%PDF-fake"
    assert _FakeLabelYandex.calls == [(999, "555")]


def test_label_route_rejects_bad_signatures_and_other_campaigns(monkeypatch):
    from sync_service.yandex_market_order_sync import label_link_url

    path = _label_route(monkeypatch)
    other_campaign = label_link_url(149179204, 999, "key").removeprefix("https://protsvetnoy.us")
    for bad in (path.replace("/999/", "/998/"), other_campaign, "/labels/../secret.pdf"):
        captured, _ = _get(bad)
        assert captured["status"] == "404 Not Found", bad
    assert _FakeLabelYandex.calls == []
