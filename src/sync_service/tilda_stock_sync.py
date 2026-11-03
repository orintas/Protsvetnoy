from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .tilda_client import TildaCommerceMLClient, transcript_text
from .yandex_market_stock_sync import AssortmentCache, _changes_summary
from .yandex_market_sync import YandexMarketSyncLog

MOSCOW = ZoneInfo("Europe/Moscow")

# Key used in stock.json and in the site's own scripts -> MoySklad store.
# Agreed with the Tilda side; renaming a key breaks the site.
SITE_STORES: dict[str, str] = {
    "main": "689a000c-a14e-11e2-9030-001b21d91495",  # Основной склад
    "aviapark": "497d98c2-7e21-11ee-0a80-0e2a000dc91f",  # ТЦ Авиапарк
    "megakhimki": "dc9b7c0e-a66d-11eb-0a80-09b9002a05ad",  # ТЦ Мега Химки
    "riviera": "e1222420-16f2-11ed-0a80-010b002a9d1c",  # ТЦ Ривьера
    "salaris": "0caf123c-6e09-11f0-0a80-00c900243b0d",  # ТЦ Саларис
}

# Products sold in Russia are owned by the "Основной" group — the spec
# named PROTSVETNOY_GROUP_ID, but that's "ProTsvetnoy OU", the European arm
# (RB003 itself is in "Основной"; checked live 2026-11-02, along with: no
# product outside "Основной" has any stock in the five stores above).
SITE_GROUP_ID = "08e6b024-d269-11e4-90a2-8ecb0004d9d2"  # "Основной"

STOCK_JSON_PATH = "data/site_stock.json"
SYNC_INTERVAL_SECONDS = 600
# The order queue (tilda_order_sync) is retried on every tick of the same
# worker, between stock runs.
WORKER_TICK_SECONDS = 30
STATE_KEY = "site"


def is_site_code(code: str) -> bool:
    """`…e` codes (MG2112e) are the European versions — never on the site."""
    return bool(code) and not code.endswith("e")


def site_products(moysklad: MoySkladClient) -> list[dict[str, str]]:
    products = [{"code": str(p.get("code") or ""), "name": str(p.get("name") or "")} for p in moysklad.products_by_group(SITE_GROUP_ID)]
    return sorted((p for p in products if is_site_code(p["code"])), key=lambda p: p["code"])


def stock_snapshot(moysklad: MoySkladClient) -> dict[str, dict[str, int]]:
    """code -> {store key: available}, available = stock − reserve, floored,
    negatives as 0. A product missing from a store's report (sold out there
    drops it from the report) simply has no key for that store."""
    snapshot: dict[str, dict[str, int]] = {}
    for key, store_id in SITE_STORES.items():
        for row in moysklad.stock_by_store(store_id):
            code = row.get("code")
            if not code:
                continue
            available = max(0, int((row.get("stock") or 0) - (row.get("reserve") or 0)))
            if available > 0:
                snapshot.setdefault(str(code), {})[key] = available
    return snapshot


def site_stock_document(snapshot: dict[str, dict[str, int]], codes: set[str], now: datetime) -> dict[str, Any]:
    items = {code: dict(snapshot[code]) for code in sorted(snapshot) if code in codes and snapshot[code]}
    return {"updated": now.astimezone(MOSCOW).isoformat(timespec="seconds"), "items": items}


def write_stock_json(document: dict[str, Any], path: str = STOCK_JSON_PATH) -> None:
    """Written next to the file and renamed into place, so the web container
    (same data volume) never serves a half-written file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, target)


CATALOG_ID = "protsvetnoy-moysklad"
CLASSIFIER_ID = "protsvetnoy-moysklad-classifier"


def _header(now: datetime) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<КоммерческаяИнформация ВерсияСхемы="2.05" ДатаФормирования="{now.astimezone(MOSCOW).strftime("%Y-%m-%dT%H:%M:%S")}">\n'
    )


def _product_ids(code: str) -> str:
    # Tilda's own SKU (= MoySklad code) is also set as its External ID, so
    # the product matches by either <Ид> or <Артикул>.
    code = escape(code)
    return f"<Ид>{code}</Ид><Артикул>{code}</Артикул>"


def import_xml(products: list[dict[str, str]], now: datetime) -> bytes:
    """Minimal catalog: just enough for Tilda to match each offer to a product.
    СодержитТолькоИзменения keeps a product that isn't listed from being
    treated as removed."""
    rows = "".join(f"<Товар>{_product_ids(p['code'])}<Наименование>{escape(p['name'])}</Наименование></Товар>\n" for p in products)
    body = (
        _header(now)
        + f"<Классификатор><Ид>{CLASSIFIER_ID}</Ид><Наименование>МойСклад</Наименование></Классификатор>\n"
        + f'<Каталог СодержитТолькоИзменения="true"><Ид>{CATALOG_ID}</Ид><ИдКлассификатора>{CLASSIFIER_ID}</ИдКлассификатора><Наименование>Каталог</Наименование>\n'
        + f"<Товары>\n{rows}</Товары>\n</Каталог>\n</КоммерческаяИнформация>\n"
    )
    return body.encode("utf-8")


def offers_xml(products: list[dict[str, str]], quantities: dict[str, int], now: datetime) -> bytes:
    """Stock only — no <Цены>: prices are kept by hand in Tilda."""
    rows = "".join(
        f"<Предложение>{_product_ids(p['code'])}<Наименование>{escape(p['name'])}</Наименование><Количество>{quantities.get(p['code'], 0)}</Количество></Предложение>\n"
        for p in products
    )
    body = (
        _header(now)
        + f'<ПакетПредложений СодержитТолькоИзменения="true"><Ид>{CATALOG_ID}#offers</Ид><Наименование>Остатки</Наименование>'
        + f"<ИдКаталога>{CATALOG_ID}</ИдКаталога><ИдКлассификатора>{CLASSIFIER_ID}</ИдКлассификатора>\n"
        + f"<Предложения>\n{rows}</Предложения>\n</ПакетПредложений>\n</КоммерческаяИнформация>\n"
    )
    return body.encode("utf-8")


def push_to_tilda(settings: Settings, products: list[dict[str, str]], quantities: dict[str, int], now: datetime) -> list[dict[str, Any]]:
    """One full CommerceML exchange; returns what Tilda answered at each step."""
    client = TildaCommerceMLClient(login=settings.tilda_cml_login, password=settings.tilda_cml_password, url=settings.tilda_cml_url)
    try:
        client.exchange([("import.xml", import_xml(products, now)), ("offers.xml", offers_xml(products, quantities, now))])
        return transcript_text(client)
    finally:
        client.close()


def tilda_push_configured(settings: Settings) -> bool:
    return settings.tilda_stock_sync_enabled and bool(settings.tilda_cml_login and settings.tilda_cml_password)


def run_once(settings: Settings, log: YandexMarketSyncLog, state: AssortmentCache, *, now: datetime | None = None,
             stock_json_path: str = STOCK_JSON_PATH) -> None:
    """One snapshot feeds both outputs: stock.json for the site's scripts
    (per store) and the CommerceML package for Tilda (sum of all five
    stores, so a product that's only in a mall can still go into the cart
    for pickup). The package is sent in full every run — it also corrects
    any count someone changed by hand in Tilda."""
    now = now or datetime.now(MOSCOW)
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    try:
        products = site_products(moysklad)
        snapshot = stock_snapshot(moysklad)
    finally:
        moysklad.close()
    codes = {p["code"] for p in products}
    document = site_stock_document(snapshot, codes, now)
    write_stock_json(document, stock_json_path)
    totals = {p["code"]: sum(snapshot.get(p["code"], {}).values()) for p in products}

    if not tilda_push_configured(settings):
        log.add("stock_sync", "success", f"stock.json обновлён: в наличии {len(document['items'])} из {len(products)}; выгрузка в Tilda выключена", None,
                {"in_stock": len(document["items"]), "products": len(products)})
        return

    old = state.last_counts(STATE_KEY)
    changes = None if not old else [
        {"sku": code, "before": old.get(code), "after": count} for code, count in totals.items() if old.get(code) != count
    ]
    transcript = push_to_tilda(settings, products, totals, now)
    state.save_counts(STATE_KEY, totals)
    log.add("stock_sync", "success", f"Tilda: отправлено {len(products)} товаров, в наличии {len(document['items'])}, {_changes_summary(changes)}", None,
            {"products": len(products), "in_stock": len(document["items"]), "changes": changes, "tilda": transcript})


def push_test(settings: Settings, codes: list[str], log: YandexMarketSyncLog) -> list[dict[str, Any]]:
    """The agreed first run: a package with just these products (RB003), to
    check in Tilda that only its stock changed — price, name and photos
    untouched. Doesn't touch the diff state the regular run keeps."""
    now = datetime.now(MOSCOW)
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    try:
        products = [p for p in site_products(moysklad) if p["code"] in set(codes)]
        snapshot = stock_snapshot(moysklad)
    finally:
        moysklad.close()
    missing = sorted(set(codes) - {p["code"] for p in products})
    if missing:
        raise ValueError(f"Не найдены среди товаров сайта в МойСклад: {', '.join(missing)}")
    totals = {p["code"]: sum(snapshot.get(p["code"], {}).values()) for p in products}
    transcript = push_to_tilda(settings, products, totals, now)
    log.add("stock_test", "success", f"Tilda, пробная выгрузка: {', '.join(f'{code}={count}' for code, count in totals.items())}", None,
            {"quantities": totals, "tilda": transcript})
    return transcript


def worker() -> None:
    """Round the clock, unlike the marketplaces — the site sells at night too."""
    from .tilda_order_sync import TildaOrderQueue, retry_pending

    settings = Settings.from_env()
    log = YandexMarketSyncLog("data/tilda_sync.sqlite3")
    state = AssortmentCache("data/tilda_stock_state.sqlite3")
    queue = TildaOrderQueue()
    errors = ErrorLog()
    last_stock_run: float | None = None
    while True:
        if last_stock_run is None or time.monotonic() - last_stock_run >= SYNC_INTERVAL_SECONDS:
            last_stock_run = time.monotonic()
            try:
                run_once(settings, log, state)
            except Exception as error:
                log.add("stock_sync", "error", f"Ошибка синхронизации остатков сайта: {error}", None)
                errors.log_exception("tilda_stock_sync", error, context="Ошибка синхронизации остатков сайта (Tilda)")
        try:
            retry_pending(settings, queue, log, errors)
        except Exception as error:
            errors.log_exception("tilda_order_sync", error, context="Ошибка повторной обработки заказов с сайта")
        time.sleep(WORKER_TICK_SECONDS)
