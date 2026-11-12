"""T-Bank acquiring → MoySklad, for the Russian stores of ООО «ЦВЕТНОЙ МИР».

One T-Bank account receives the money of two MoySklad accounts: the main
one (Цветной stores) and the hats store's own MoySklad. Every day, once it
is over:
1. every card operation (T-API, per terminal) and every SBP payment (account
   statement) is matched to a receipt in the store's MoySklad — a payment
   matching a receipt of another payment type, a payment without a receipt
   and a receipt without a payment are reported (journal «Эквайринг» +
   error journal);
2. per store, one Входящий платёж for the day's card money and one for its
   SBP money, at the full amount, linked to that day's retail shift — the
   way it was done by hand until 2026-10-31 (Рябцева В. / Муминова Г. С.),
   except the bank's fee is not netted out but booked separately as
3. one Исходящий платёж per MoySklad account per day for its share of the fee.

Card money arrives as one credit per day for every terminal and also
carries what T-API doesn't list: phone (SoftPOS) payments of
`phone_payments_store` and QR payments taken on any store's card terminal;
that difference is matched to receipts left without a payment.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .acquiring_reconcile import (
    MOSCOW,
    DayResult,
    Finding,
    Payment,
    Receipt,
    StatementLine,
    absorb_payout_residual,
    parse_card_operation,
    parse_receipt,
    parse_statement_line,
    reconcile_day,
    rub,
    sbp_payment,
)
from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .moysklad_links import moysklad_link_fields
from .tbank_client import TBankClient
from .yandex_market_sync import YandexMarketSyncLog

LOG_PATH = "data/acquiring_sync.sqlite3"
LOOKBACK_DAYS = 10
# T-API card operations may lag up to two hours — a day is checked only once
# it is safely over.
DAY_SETTLED_HOUR = 3
# The card payout for day D is credited in the night to D+1 (Monday for a
# weekend, confirmed live); a day still without one after this is reported.
PAYOUT_GRACE_DAYS = 4
RUN_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class AcquiringStore:
    name: str
    retail_store_id: str
    terminal: str
    account: str  # AcquiringAccount.key


@dataclass(frozen=True)
class AcquiringAccount:
    key: str
    name: str
    token_env: str
    organization_id: str
    organization_account_id: str
    group_id: str
    card_agent: dict[str, str]
    sbp_agent: dict[str, str]
    fee_agent: dict[str, str]
    fee_expense_item_id: str


@dataclass(frozen=True)
class AcquiringConfig:
    account_number: str
    start_day: date
    phone_payments_store: str
    sbp_terminals: dict[str, str]
    accounts: tuple[AcquiringAccount, ...]
    stores: tuple[AcquiringStore, ...]

    def store(self, retail_store_id: str) -> AcquiringStore | None:
        return next((s for s in self.stores if s.retail_store_id == retail_store_id), None)

    def store_name(self, retail_store_id: str) -> str:
        store = self.store(retail_store_id)
        return store.name if store else "магазин не определён"

    def account(self, key: str) -> AcquiringAccount:
        return next(a for a in self.accounts if a.key == key)


def load_config(path: Path | str = "config/acquiring-stores.json") -> AcquiringConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    accounts, stores = [], []
    for item in raw["accounts"]:
        accounts.append(AcquiringAccount(
            key=item["key"], name=item["name"], token_env=item["token_env"],
            organization_id=item["organization_id"], organization_account_id=item["organization_account_id"],
            group_id=item["group_id"], card_agent=item["card_agent"], sbp_agent=item["sbp_agent"],
            fee_agent=item["fee_agent"], fee_expense_item_id=item["fee_expense_item_id"],
        ))
        stores += [AcquiringStore(s["name"], s["retail_store_id"], str(s.get("terminal") or ""), item["key"]) for s in item["stores"]]
    return AcquiringConfig(
        account_number=raw["account_number"],
        start_day=date.fromisoformat(raw["start_day"]),
        phone_payments_store=raw.get("phone_payments_store", ""),
        sbp_terminals=dict(raw.get("sbp_terminals") or {}),
        accounts=tuple(accounts),
        stores=tuple(stores),
    )


class AcquiringLog(YandexMarketSyncLog):
    def __init__(self, path: str = LOG_PATH) -> None:
        super().__init__(path)

    def add_once(self, kind: str, status: str, message: str, external_id: str, payload: Any = None) -> bool:
        """Like add(), but reports whether the row is new — so a finding goes
        to the error journal once, not on every hourly re-run."""
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO sync_log(created_at,kind,external_id,status,message,payload) VALUES(?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), kind, external_id, status, message, json.dumps(payload, ensure_ascii=False, default=str)),
            )
            return cursor.rowcount > 0


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=MOSCOW)
    return start, start + timedelta(days=1)


def _moment(value: datetime) -> str:
    return value.astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M:%S.000")


def _agent_id(document: dict[str, Any]) -> str:
    return str(((document.get("agent") or {}).get("meta") or {}).get("href", "")).rsplit("/", 1)[-1]


def _linked_shifts(document: dict[str, Any]) -> set[str]:
    return {
        str(op["meta"]["href"]).rsplit("/", 1)[-1]
        for op in document.get("operations") or []
        if isinstance(op, dict) and (op.get("meta") or {}).get("type") == "retailshift"
    }


@dataclass
class DayPlan:
    """Everything decided for one day, before anything is written."""
    day: date
    result: DayResult
    card_payout: StatementLine | None = None
    card_fee: StatementLine | None = None
    card_share: dict[str, float] = field(default_factory=dict)  # account key -> share of the card payout
    sbp_fees: dict[str, int] = field(default_factory=dict)  # account key -> SBP fees of its payments
    final: bool = False


class Run:
    def __init__(self, *, config: AcquiringConfig, moysklad: dict[str, MoySkladClient], tbank: TBankClient, log: AcquiringLog,
                 errors: ErrorLog, dry_run: bool, now: datetime) -> None:
        self.config = config
        self.moysklad = moysklad
        self.tbank = tbank
        self.log = log
        self.errors = errors
        self.dry_run = dry_run
        self.now = now.astimezone(MOSCOW)

    def error(self, kind: str, message: str, external_id: str, payload: Any = None) -> None:
        if self.log.add_once(kind, "error", message, external_id, payload) and not self.dry_run:
            self.errors.add("acquiring", message, json.dumps(payload, ensure_ascii=False, default=str) if payload else "")

    # -- inputs ------------------------------------------------------------
    def days(self) -> list[date]:
        last = self.now.date() - timedelta(days=1 if self.now.hour >= DAY_SETTLED_HOUR else 2)
        first = max(self.config.start_day, self.now.date() - timedelta(days=LOOKBACK_DAYS))
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    def terminal_keys(self) -> dict[str, str]:
        """T-API terminal key -> retail_store_id, for terminals listed as active."""
        listed = {str(t.get("key")) for t in self.tbank.terminals()}
        ours = {s.terminal: s.retail_store_id for s in self.config.stores if s.terminal}
        unknown = sorted(listed - set(ours))
        if unknown:
            self.error("terminal_error", f"В ТБанке есть терминалы, не привязанные к магазину в config/acquiring-stores.json: {', '.join(unknown)}. "
                                         "Их оплаты не сверяются и будут считаться оплатами на телефоне.", f"unknown-terminals:{','.join(unknown)}")
        return {key: store for key, store in ours.items() if key in listed}

    def receipts(self, day: date) -> list[Receipt]:
        result: list[Receipt] = []
        for store in self.config.stores:
            client = self.moysklad.get(store.account)
            if client is None:
                continue
            href = f"{client.base_url}/entity/retailstore/{store.retail_store_id}"
            day_filter = f"retailStore={href};moment>={day} 00:00:00;moment<={day} 23:59:59"
            result += [parse_receipt(store.retail_store_id, "sale", row) for row in client.documents("retaildemand", document_filter=day_filter)]
            result += [parse_receipt(store.retail_store_id, "return", row) for row in client.documents("retailsalesreturn", document_filter=day_filter)]
        return result

    # -- planning ----------------------------------------------------------
    def plan_day(self, day: date, keys: dict[str, str], lines: list[StatementLine]) -> DayPlan:
        start, end = _day_bounds(day)
        connected = {s.retail_store_id for s in self.config.stores if s.account in self.moysklad}
        card_ops: list[Payment] = []
        debits: dict[str, int] = {}  # account key -> card payments (refunds are debited by the bank separately)
        for key, store_id in keys.items():
            account = self.config.store(store_id).account
            for row in self.tbank.terminal_operations(key, start, end):
                payment = parse_card_operation(row, store_id)
                if not payment.refund:
                    debits[account] = debits.get(account, 0) + payment.amount
                if store_id in connected:
                    card_ops.append(payment)
        sbp_lines = [line for line in lines if line.kind == "sbp" and line.booked_at.date() == day]
        payments = card_ops + [sbp_payment(line, self.config.sbp_terminals.get(line.terminal)) for line in sbp_lines]
        result = reconcile_day(payments, self.receipts(day), card_stores={s.retail_store_id for s in self.config.stores if s.terminal and s.retail_store_id in connected})
        plan = DayPlan(day=day, result=result)

        next_day = day + timedelta(days=1)
        plan.card_payout = next((l for l in lines if l.kind == "card_payout" and l.payout_day == next_day), None)
        plan.card_fee = next((l for l in lines if l.kind == "card_fee" and l.payout_day == next_day), None)
        all_debits = sum(debits.values())
        if plan.card_payout is not None:
            residual = plan.card_payout.amount - all_debits
            if residual < 0:
                result.findings.append(Finding(
                    "card_payout_short", "",
                    f"Зачислено по картам {rub(plan.card_payout.amount)}, а по терминалам оплат на {rub(all_debits)} — банк недоплатил {rub(-residual)}",
                    f"payout:{plan.card_payout.operation_id}"))
            elif residual > 0:
                before = {store: dict(amounts) for store, amounts in result.assigned.items()}
                unexplained = absorb_payout_residual(result, residual, self.config.phone_payments_store)
                for store_id, amounts in result.assigned.items():
                    gained = amounts["card"] - before.get(store_id, {}).get("card", 0)
                    if gained:
                        account = self.config.store(store_id).account
                        debits[account] = debits.get(account, 0) + gained
                if unexplained and self.config.phone_payments_store:
                    phone_account = self.config.store(self.config.phone_payments_store).account
                    debits[phone_account] = debits.get(phone_account, 0) + unexplained
                if unexplained:
                    result.findings.append(Finding(
                        "card_payout_unexplained", "",
                        f"В зачислении по картам {rub(unexplained)} без операции в T-API (оплата на телефоне в Мега Химки "
                        f"или по QR на терминале любой точки), и чеков без оплаты на эту сумму нет — не разнесено ни в одну точку, разнесите вручную",
                        f"payout:{plan.card_payout.operation_id}"))
            total = sum(debits.values())
            plan.card_share = {account: amount / total for account, amount in debits.items()} if total else {}
            plan.final = True
        elif (self.now.date() - day).days >= PAYOUT_GRACE_DAYS:
            if all_debits:
                result.findings.append(Finding("card_payout_missing", "", f"Оплат картой на {rub(all_debits)}, а зачисления от банка за этот день нет", "payout:missing"))
            plan.final = True
        sbp_owner: dict[str, str] = {}
        for p, receipts in result.matched:
            if p.channel == "qr":
                sbp_owner[p.ref.removeprefix("sbp:")] = self.config.store(receipts[0].store).account
        for f in result.findings:
            if f.code.startswith("qr_as_") and f.store:
                sbp_owner[f.ref.removeprefix("sbp:")] = self.config.store(f.store).account
        for line in lines:
            if line.kind == "sbp_fee" and line.sbp_id in sbp_owner:
                account = sbp_owner[line.sbp_id]
                plan.sbp_fees[account] = plan.sbp_fees.get(account, 0) + line.amount
        return plan

    # -- reporting ---------------------------------------------------------
    def report(self, plan: DayPlan) -> None:
        for finding in plan.result.findings:
            store = self.config.store_name(finding.store) if finding.store else "Все точки"
            message = f"{store}, {plan.day:%d.%m.%Y}: {finding.message}"
            payload = {"day": plan.day.isoformat(), "code": finding.code, "store": store,
                       **({"moysklad_url": finding.receipt_url, "moysklad_label": "Открыть чек"} if finding.receipt_url else {})}
            self.error("mismatch", message, f"{plan.day}:{finding.code}:{finding.store}:{finding.ref}", payload)

    # -- booking -----------------------------------------------------------
    def book(self, plan: DayPlan, existing: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]]) -> None:
        for store_id, amounts in sorted(plan.result.assigned.items()):
            store = self.config.store(store_id)
            if store is None or store.account not in self.moysklad:
                continue
            account = self.config.account(store.account)
            client = self.moysklad[store.account]
            existing_in, existing_out = existing[store.account]
            codes = {str(p.get("externalCode") or "") for p in existing_in}
            for channel in ("card", "qr"):
                amount = amounts.get(channel, 0)
                if amount == 0:
                    continue
                what = "картам" if channel == "card" else "СБП"
                external_code = f"tbank-{channel}-{store_id}-{plan.day}"
                if external_code in codes:
                    continue
                if amount < 0:
                    self.error("payment_error", f"{store.name}, {plan.day:%d.%m.%Y}: возвраты по {what} превышают оплаты ({rub(amount)}) — платёж не создан, разнесите вручную", external_code)
                    continue
                shift = client.retail_shift_for_day(store_id, plan.day.isoformat())
                if shift is None:
                    self.error("payment_error", f"{store.name}, {plan.day:%d.%m.%Y}: оплат по {what} на {rub(amount)}, а смены за этот день в МойСклад нет — входящий платёж не создан", external_code)
                    continue
                agent = account.card_agent if channel == "card" else account.sbp_agent
                manual = [p for p in existing_in if str(shift["id"]) in _linked_shifts(p) and _agent_id(p) == agent["id"]
                          and not str(p.get("externalCode") or "").startswith("tbank-")]
                if manual:
                    self.log.add_once("payment_manual", "success",
                                      f"{store.name}, {plan.day:%d.%m.%Y}: к смене №{shift.get('name')} уже есть платёж, разнесённый вручную (№{manual[0].get('name')}) — по {what} не создаётся",
                                      external_code, moysklad_link_fields(manual[0], f"Платёж №{manual[0].get('name')}"))
                    continue
                moment = plan.card_payout.booked_at if channel == "card" and plan.card_payout else _day_bounds(plan.day)[1] - timedelta(minutes=1)
                description = f"ТБанк, оплаты по {what} за {plan.day:%d.%m.%Y}, {store.name} — полная сумма, комиссия отдельным платежом. Создано сервисом синхронизации."
                if self.dry_run:
                    self.log.add_once("payment", "dry-run", f"{store.name}, {plan.day:%d.%m.%Y}: был бы создан входящий платёж по {what} на {rub(amount)} к смене №{shift.get('name')}", external_code)
                    continue
                payment = client.create_payment_in(
                    organization_id=account.organization_id, organization_account_id=account.organization_account_id,
                    group_id=account.group_id, agent_type=agent["type"], agent_id=agent["id"], amount=amount,
                    moment=_moment(moment), retail_shift_id=str(shift["id"]), external_code=external_code, description=description,
                )
                existing_in.append(payment)
                self.log.add("payment", "success", f"{store.name}, {plan.day:%d.%m.%Y}: создан входящий платёж №{payment.get('name')} по {what} на {rub(amount)} к смене №{shift.get('name')}",
                             external_code, moysklad_link_fields(payment, f"Платёж №{payment.get('name')}"))
        for account in self.config.accounts:
            if account.key in self.moysklad:
                self.book_fee(plan, account, existing[account.key][1])

    def book_fee(self, plan: DayPlan, account: AcquiringAccount, existing_out: list[dict[str, Any]]) -> None:
        share = plan.card_share.get(account.key, 0.0)
        card_fee = round(plan.card_fee.amount * share) if plan.card_fee else 0
        sbp_fee = plan.sbp_fees.get(account.key, 0)
        total = card_fee + sbp_fee
        external_code = f"tbank-fee-{account.key}-{plan.day}"
        if total <= 0 or any(str(p.get("externalCode") or "") == external_code for p in existing_out):
            return
        payout_day = plan.day + timedelta(days=1)
        if plan.card_fee and any(f"эквайринга от {payout_day:%d.%m.%Y}" in str(p.get("paymentPurpose") or "") for p in existing_out):
            self.log.add_once("fee_manual", "success", f"{account.name}, {plan.day:%d.%m.%Y}: комиссия эквайринга за этот день уже есть в МойСклад — пропущено", external_code)
            return
        parts = []
        if card_fee:
            parts.append(f"по картам {rub(card_fee)}" + (f" ({share:.1%} от общей {rub(plan.card_fee.amount)})" if share < 0.9995 else ""))
        if sbp_fee:
            parts.append(f"по СБП {rub(sbp_fee)}")
        purpose = f"Комиссия ТБанка за эквайринг за {plan.day:%d.%m.%Y}: " + ", ".join(parts)
        moment = plan.card_fee.booked_at if plan.card_fee else _day_bounds(plan.day)[1] - timedelta(minutes=1)
        if self.dry_run:
            self.log.add_once("fee", "dry-run", f"{account.name}, {plan.day:%d.%m.%Y}: был бы создан исходящий платёж на {rub(total)} — {purpose}", external_code)
            return
        payment = self.moysklad[account.key].create_payment_out(
            organization_id=account.organization_id, organization_account_id=account.organization_account_id,
            group_id=account.group_id, agent_type=account.fee_agent["type"], agent_id=account.fee_agent["id"],
            expense_item_id=account.fee_expense_item_id, amount=total, moment=_moment(moment),
            external_code=external_code, payment_purpose=purpose, description="Создано сервисом синхронизации эквайринга ТБанк.",
        )
        existing_out.append(payment)
        self.log.add("fee", "success", f"{account.name}, {plan.day:%d.%m.%Y}: создан исходящий платёж №{payment.get('name')} на {rub(total)} — {purpose}",
                     external_code, moysklad_link_fields(payment, f"Платёж №{payment.get('name')}"))

    # -- run ---------------------------------------------------------------
    def execute(self) -> int:
        days = [d for d in self.days() if not self.log.has_success("day", d.isoformat())]
        if not days:
            return 0
        keys = self.terminal_keys()
        start = _day_bounds(days[0])[0]
        lines = [l for l in (parse_statement_line(row) for row in self.tbank.statement(self.config.account_number, start, self.now)) if l]
        existing: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
        for account in self.config.accounts:
            client = self.moysklad.get(account.key)
            if client is None:
                continue
            since = f"organization={client.base_url}/entity/organization/{account.organization_id};moment>={days[0]} 00:00:00"
            existing[account.key] = (client.documents("paymentin", document_filter=since), client.documents("paymentout", document_filter=since))
        finished = 0
        for day in days:
            plan = self.plan_day(day, keys, lines)
            if not plan.final:
                continue  # the card payout isn't credited yet — next run
            self.report(plan)
            self.book(plan, existing)
            errors = len(plan.result.findings)
            summary = {"assigned": {self.config.store_name(k): {c: v / 100 for c, v in a.items() if v} for k, a in plan.result.assigned.items() if k}}
            message = f"{day:%d.%m.%Y}: сопоставлено оплат {len(plan.result.matched)}, расхождений {errors}"
            if self.dry_run:
                # status "dry-run" never counts as done (has_success), so the day
                # is checked for real once dry run is switched off
                self.log.add_once("day", "dry-run", message + " (тестовый режим — платежи не создаются)", f"dry-run:{day}", summary)
            else:
                self.log.add("day", "success", message, day.isoformat(), summary)
            finished += 1
        return finished


def moysklad_clients(settings: Settings, config: AcquiringConfig, errors: ErrorLog | None = None) -> dict[str, MoySkladClient]:
    clients: dict[str, MoySkladClient] = {}
    for account in config.accounts:
        token = settings.moysklad_token if account.token_env == "MOYSKLAD_TOKEN" else os.getenv(account.token_env, "")
        if token:
            clients[account.key] = MoySkladClient(base_url=settings.moysklad_base_url, token=token)
        elif errors is not None:
            errors.add("acquiring", f"{account.token_env} не задан — {account.name} в сверку эквайринга не входит", level="info")
    return clients


def run_once(settings: Settings, log: AcquiringLog, errors: ErrorLog, *, now: datetime | None = None, dry_run: bool | None = None) -> int:
    config = load_config()
    clients = moysklad_clients(settings, config)
    tbank = TBankClient(token=settings.tbank_api_token, base_url=settings.tbank_base_url)
    try:
        return Run(config=config, moysklad=clients, tbank=tbank, log=log, errors=errors,
                   dry_run=settings.tbank_acquiring_dry_run if dry_run is None else dry_run,
                   now=now or datetime.now(MOSCOW)).execute()
    finally:
        for client in clients.values():
            client.close()
        tbank.close()


def worker() -> None:
    settings = Settings.from_env()
    log = AcquiringLog()
    errors = ErrorLog()
    if not settings.tbank_api_token:
        errors.add("acquiring", "TBANK_API_KEY не задан — сверка эквайринга ТБанк не запускается", level="info")
        while True:
            time.sleep(RUN_INTERVAL_SECONDS)
    while True:
        try:
            run_once(settings, log, errors)
        except Exception as error:
            errors.log_exception("acquiring_sync_worker", error, context="Ошибка сверки эквайринга ТБанк")
        time.sleep(RUN_INTERVAL_SECONDS)
