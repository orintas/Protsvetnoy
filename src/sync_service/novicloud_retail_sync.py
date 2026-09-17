from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .novicloud import NovicloudClient
from .store_mapping import StoreMapping, load_store_mappings
from .sync_log import SyncLog

# Sales sync only runs during the shops' opening hours — outside 9:00-23:00
# there's nothing new to find, so it skips hitting the Novicloud API. Returns
# aren't restricted (only sales were called out by name).
WARSAW = ZoneInfo("Europe/Warsaw")
SALES_SYNC_START_HOUR = 9
SALES_SYNC_END_HOUR = 23


def _within_sales_sync_window(now: datetime | None = None) -> bool:
    local = (now or datetime.now(WARSAW)).astimezone(WARSAW)
    return SALES_SYNC_START_HOUR <= local.hour < SALES_SYNC_END_HOUR

# Ported from the "Sales from Navicloud to MoySklad" / "Returns from Navicloud
# to MoySklad" Make.com scenarios (both disabled 2026-09-15 once this went
# live) — field names, document type codes and the cash/non-cash payment
# split are copied from there, confirmed against the live Novicloud API.
SALE_DOC_TYPES = "21,112"
RETURN_DOC_TYPE = "8"
NON_CASH_PAYMENT_FORM_ID = 2

# Novicloud's rate limit is stricter than MoySklad's — Make's original
# scenario put a 20s wait between each document; this is a lighter but still
# conservative gap between the per-document/per-position/per-product calls.
RATE_LIMIT_SLEEP_SECONDS = 2


def _to_novicloud_date(moysklad_moment: str) -> str:
    """MoySklad "2026-09-10 10:31:45.000" -> Novicloud "2026-09-10T10:31:45"."""
    dt = datetime.strptime(moysklad_moment.split(".")[0], "%Y-%m-%d %H:%M:%S")
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _as_int(raw: Any) -> int | None:
    """MoySklad's documentNumber/checkNumber fields are strict integers —
    the matching Novicloud fields aren't guaranteed numeric, so fall back to
    omitting them rather than sending a value the API rejects with HTTP 400."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _to_moysklad_moment(novicloud_date: str | None) -> str:
    if not novicloud_date:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.000")
    dt = datetime.fromisoformat(novicloud_date)
    return dt.strftime("%Y-%m-%d %H:%M:%S.000")


def _payment_split(doc: dict[str, Any], *, negate: bool) -> tuple[int, int]:
    """(cashSum, noCashSum) in kopecks/groszy, matching MoySklad's *100 minor-unit convention."""
    total_paid = doc.get("zaplacono") or 0
    non_cash = sum(
        payment.get("wplata_waluta") or 0
        for payment in doc.get("platnosci") or []
        if (payment.get("forma_platnosci") or {}).get("id") == NON_CASH_PAYMENT_FORM_ID
    )
    cash = total_paid - non_cash
    sign = -1 if negate else 1
    return round(cash * 100 * sign), round(non_cash * 100 * sign)


def _build_positions(moysklad: MoySkladClient, novicloud: NovicloudClient, positions_link: str) -> tuple[list[dict[str, Any]], list[str]]:
    payload = novicloud.get_url(positions_link)
    positions: list[dict[str, Any]] = []
    missing: list[str] = []
    for row in payload.get("dane", []) or []:
        if row.get("storno"):
            continue
        towar_link = (row.get("towar") or {}).get("link")
        quantity = row.get("ilosc") or 0
        if not towar_link or not quantity:
            continue
        product = (novicloud.get_url(towar_link).get("dane") or {})
        moysklad_uuid = product.get("opis_3")
        if not moysklad_uuid:
            missing.append(str(product.get("kod") or towar_link))
            continue
        positions.append({
            "quantity": quantity,
            "price": round((row.get("w_brutto") or 0) / quantity * 100),
            "vat": round((row.get("stawka_vat") or 0) / 100),
            "assortment": {"meta": {"href": f"{moysklad.base_url}/entity/product/{moysklad_uuid}", "type": "product", "mediaType": "application/json"}},
        })
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    return positions, missing


def _ensure_open_shift(moysklad: MoySkladClient, store: StoreMapping) -> str:
    shift = moysklad.find_open_retail_shift(store.retail_store_id)
    if shift is not None:
        return str(shift["id"])
    created = moysklad.create_retail_shift(
        organization_id=store.organization_id,
        store_id=store.moysklad_store_id,
        retail_store_id=store.retail_store_id,
        department_id=store.department_id,
        owner_id=store.owner_id,
    )
    return str(created["id"])


def sync_store_sales(moysklad: MoySkladClient, novicloud: NovicloudClient, store: StoreMapping, log: SyncLog) -> int:
    last_moment = moysklad.last_document_moment("retaildemand", store.retail_store_id)
    date_from = _to_novicloud_date(last_moment) if last_moment else None
    docs = novicloud.documents(typ_dok=SALE_DOC_TYPES, sklep_id=store.novicloud_store_id, date_from=date_from).get("dane", []) or []

    created = 0
    shift_id: str | None = None
    for doc in docs:
        nr_dok = doc.get("nr_dok")
        if not nr_dok or doc.get("storno"):
            continue
        if moysklad.document_exists("retaildemand", name=nr_dok, retail_store_id=store.retail_store_id):
            continue

        positions, missing = _build_positions(moysklad, novicloud, (doc.get("pozycje") or {}).get("link", ""))
        if missing:
            log.add("sale_error", "error", f"{store.name}: чек {nr_dok} — товары без сопоставления с МойСклад (нет opis_3): {', '.join(missing)}", nr_dok, doc)
        if not positions:
            continue

        if shift_id is None:
            shift_id = _ensure_open_shift(moysklad, store)
        cash_sum, non_cash_sum = _payment_split(doc, negate=False)
        moysklad.create_retail_demand(
            name=nr_dok,
            moment=_to_moysklad_moment(doc.get("data_wystawienia")),
            document_number=_as_int(doc.get("nr_systemowy")),
            check_number=_as_int(doc.get("nr_fiskalny")),
            organization_id=store.organization_id,
            store_id=store.moysklad_store_id,
            retail_store_id=store.retail_store_id,
            retail_shift_id=shift_id,
            department_id=store.department_id,
            owner_id=store.owner_id,
            currency_id=store.currency_id,
            positions=positions,
            cash_sum=cash_sum,
            non_cash_sum=non_cash_sum,
        )
        log.add("sale", "success", f"{store.name}: чек {nr_dok} создан в МойСклад ({len(positions)} позиций)", nr_dok, doc)
        created += 1
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    return created


def sync_store_returns(moysklad: MoySkladClient, novicloud: NovicloudClient, store: StoreMapping, log: SyncLog) -> int:
    last_moment = moysklad.last_document_moment("retailsalesreturn", store.retail_store_id)
    date_from = _to_novicloud_date(last_moment) if last_moment else None
    docs = novicloud.documents(typ_dok=RETURN_DOC_TYPE, sklep_id=store.novicloud_store_id, date_from=date_from).get("dane", []) or []

    created = 0
    shift_id: str | None = None
    for doc in docs:
        nr_dok = doc.get("nr_dok")
        if not nr_dok:
            continue
        if moysklad.document_exists("retailsalesreturn", name=nr_dok, retail_store_id=store.retail_store_id):
            continue

        positions, missing = _build_positions(moysklad, novicloud, (doc.get("pozycje") or {}).get("link", ""))
        if missing:
            log.add("return_error", "error", f"{store.name}: возврат {nr_dok} — товары без сопоставления с МойСклад (нет opis_3): {', '.join(missing)}", nr_dok, doc)
        if not positions:
            continue

        if shift_id is None:
            shift_id = _ensure_open_shift(moysklad, store)
        cash_sum, non_cash_sum = _payment_split(doc, negate=True)
        moysklad.create_retail_return(
            name=nr_dok,
            moment=_to_moysklad_moment(doc.get("data_wystawienia")),
            organization_id=store.organization_id,
            store_id=store.moysklad_store_id,
            retail_store_id=store.retail_store_id,
            retail_shift_id=shift_id,
            department_id=store.department_id,
            owner_id=store.owner_id,
            currency_id=store.currency_id,
            positions=positions,
            cash_sum=cash_sum,
            non_cash_sum=non_cash_sum,
        )
        log.add("return", "success", f"{store.name}: возврат {nr_dok} создан в МойСклад ({len(positions)} позиций)", nr_dok, doc)
        created += 1
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    return created


def run_once(settings: Settings, log: SyncLog) -> None:
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    novicloud = NovicloudClient(
        base_url=settings.novicloud_base_url,
        version=settings.novicloud_api_version,
        account=settings.novicloud_account,
        password=settings.novicloud_password,
    )
    total_sales = 0
    total_returns = 0
    sales_sync_active = _within_sales_sync_window()
    try:
        for store in load_store_mappings():
            if sales_sync_active:
                try:
                    total_sales += sync_store_sales(moysklad, novicloud, store, log)
                except Exception as error:
                    log.add("sale_error", "error", f"{store.name}: ошибка синхронизации продаж: {error}", None)
            try:
                total_returns += sync_store_returns(moysklad, novicloud, store, log)
            except Exception as error:
                log.add("return_error", "error", f"{store.name}: ошибка синхронизации возвратов: {error}", None)
        window_note = "" if sales_sync_active else " (продажи вне окна синхронизации 9:00–23:00 по Варшаве)"
        log.add("run", "success", f"Проверка завершена: новых чеков {total_sales}, возвратов {total_returns}{window_note}")
    finally:
        moysklad.close()
        novicloud.close()


def worker() -> None:
    settings = Settings.from_env()
    log = SyncLog()
    errors = ErrorLog()
    while True:
        try:
            run_once(settings, log)
        except Exception as error:
            errors.log_exception("novicloud_retail_sync_worker", error, context="Ошибка синхронизации продаж/возвратов Novicloud")
        time.sleep(900)
