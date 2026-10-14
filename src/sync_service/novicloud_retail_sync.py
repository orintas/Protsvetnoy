from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .moysklad_links import moysklad_link_fields
from .novicloud import NovicloudClient
from .store_mapping import StoreMapping, load_store_mappings
from .sync_log import SyncLog

# Sync only runs during the shops' opening hours — outside 9:00-23:00 the
# shops are closed, so there can be no new sales or returns to find; skips
# hitting Novicloud/MoySklad entirely rather than checking for nothing.
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


def _shift_for_day(moysklad: MoySkladClient, store: StoreMapping, moment: str, cache: dict[str, str]) -> str:
    """The store's shift for the calendar day of `moment`, creating one (opened at `moment`) if that day has none yet.

    One shift per day, never "whichever shift happens to be open": the Make
    scenario this was ported from reused any open shift, so once a nightly
    close was missed (2026-08-24) every later receipt piled into that one
    shift for weeks. Receipts reach us late, so a day's shift may already
    be closed by the time its last receipts arrive — they still belong in it.
    """
    day = moment[:10]
    if day not in cache:
        shift = moysklad.retail_shift_for_day(store.retail_store_id, day)
        if shift is None:
            shift = moysklad.create_retail_shift(
                organization_id=store.organization_id,
                store_id=store.moysklad_store_id,
                retail_store_id=store.retail_store_id,
                department_id=store.department_id,
                owner_id=store.owner_id,
                moment=moment,
            )
        cache[day] = str(shift["id"])
    return cache[day]


def close_stale_shifts(moysklad: MoySkladClient, store: StoreMapping, log: SyncLog, *, today: str) -> int:
    """Close the store's shifts left open from a previous day, each at the end of its own day."""
    closed = 0
    for shift in moysklad.stale_open_retail_shifts(store.retail_store_id, today):
        close_date = f"{str(shift['moment'])[:10]} 23:59:59.000"
        moysklad.close_retail_shift(str(shift["id"]), close_date)
        log.add("shift", "success", f"{store.name}: закрыта смена №{shift.get('name')} за {close_date[:10]}", shift.get("name"),
                {**shift, **moysklad_link_fields(shift, f"Смена №{shift.get('name')}")})
        closed += 1
    return closed


def sync_store_sales(moysklad: MoySkladClient, novicloud: NovicloudClient, store: StoreMapping, log: SyncLog) -> int:
    last_moment = moysklad.last_document_moment("retaildemand", store.retail_store_id)
    date_from = _to_novicloud_date(last_moment) if last_moment else None
    docs = novicloud.documents(typ_dok=SALE_DOC_TYPES, sklep_id=store.novicloud_store_id, date_from=date_from).get("dane", []) or []

    created = 0
    shifts: dict[str, str] = {}
    for doc in sorted(docs, key=lambda d: d.get("data_wystawienia") or ""):
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

        moment = _to_moysklad_moment(doc.get("data_wystawienia"))
        shift_id = _shift_for_day(moysklad, store, moment, shifts)
        cash_sum, non_cash_sum = _payment_split(doc, negate=False)
        demand = moysklad.create_retail_demand(
            name=nr_dok,
            moment=moment,
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
        log.add("sale", "success", f"{store.name}: чек {nr_dok} создан в МойСклад ({len(positions)} позиций)", nr_dok, {**doc, **moysklad_link_fields(demand, f"Чек {nr_dok}")})
        created += 1
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    return created


def sync_store_returns(moysklad: MoySkladClient, novicloud: NovicloudClient, store: StoreMapping, log: SyncLog) -> int:
    last_moment = moysklad.last_document_moment("retailsalesreturn", store.retail_store_id)
    date_from = _to_novicloud_date(last_moment) if last_moment else None
    docs = novicloud.documents(typ_dok=RETURN_DOC_TYPE, sklep_id=store.novicloud_store_id, date_from=date_from).get("dane", []) or []

    created = 0
    shifts: dict[str, str] = {}
    for doc in sorted(docs, key=lambda d: d.get("data_wystawienia") or ""):
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

        moment = _to_moysklad_moment(doc.get("data_wystawienia"))
        shift_id = _shift_for_day(moysklad, store, moment, shifts)
        cash_sum, non_cash_sum = _payment_split(doc, negate=True)
        sales_return = moysklad.create_retail_return(
            name=nr_dok,
            moment=moment,
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
        log.add("return", "success", f"{store.name}: возврат {nr_dok} создан в МойСклад ({len(positions)} позиций)", nr_dok, {**doc, **moysklad_link_fields(sales_return, f"Возврат {nr_dok}")})
        created += 1
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    return created


def run_once(settings: Settings, log: SyncLog) -> None:
    if not _within_sales_sync_window():
        log.add("run", "success", "Проверка пропущена: магазины закрыты (вне окна 9:00–23:00 по Варшаве)")
        return

    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    novicloud = NovicloudClient(
        base_url=settings.novicloud_base_url,
        version=settings.novicloud_api_version,
        account=settings.novicloud_account,
        password=settings.novicloud_password,
    )
    total_sales = 0
    total_returns = 0
    try:
        for store in load_store_mappings():
            try:
                total_sales += sync_store_sales(moysklad, novicloud, store, log)
            except Exception as error:
                log.add("sale_error", "error", f"{store.name}: ошибка синхронизации продаж: {error}", None)
            try:
                total_returns += sync_store_returns(moysklad, novicloud, store, log)
            except Exception as error:
                log.add("return_error", "error", f"{store.name}: ошибка синхронизации возвратов: {error}", None)
            try:
                close_stale_shifts(moysklad, store, log, today=datetime.now(WARSAW).date().isoformat())
            except Exception as error:
                log.add("shift_error", "error", f"{store.name}: ошибка закрытия смен прошлых дней: {error}", None)
        log.add("run", "success", f"Проверка завершена: новых чеков {total_sales}, возвратов {total_returns}")
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
