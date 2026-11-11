"""Pure reconciliation logic: T-Bank payments vs MoySklad retail receipts.

No I/O here — acquiring_sync.py fetches the data, this module decides what
matches and what is wrong.

What T-Bank gives us (confirmed live 2026-11-11 on ООО «ЦВЕТНОЙ МИР»'s account):
- card payments: every operation of every terminal via T-API (time, amount,
  masked card); the money itself arrives as ONE gross credit per day for all
  terminals ("Зачисление средств по терминалам эквайринга от DD.MM.YYYY" =
  the previous day's operations), the fee is debited separately and card
  refunds are debited separately too;
- SBP (QR) payments: each one is its own gross credit on the account
  ("Пополнение по операции СБП <id>. Терминал <name>"), with its own fee
  debit — the terminal name doesn't always tell the store ("tsvetnoy.shop").

So both channels are checked payment by payment against receipts: same
amount, close in time. A payment matching a receipt of another payment type
is the cashier choosing the wrong type — the main thing to report.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")

# A receipt is punched within minutes of the payment; beyond this a
# same-amount pairing is still accepted, but only as a second pass.
MATCH_WINDOW = timedelta(minutes=15)
# One payment split over several receipts, or one receipt paid with several
# cards — confirmed live (10 000 = 5 990 + 4 010; 9 280 = 7 090 + 2 190).
MAX_SPLIT = 3
# Split payments are looked for further from the receipt: the cashier first
# takes both cards, then punches one receipt (confirmed live: 16 and 50 min).
SPLIT_WINDOW = timedelta(minutes=60)

CHANNEL_WORDS = {"card": "картой", "qr": "по СБП (QR)", "cash": "наличными"}


@dataclass(frozen=True)
class Payment:
    channel: str  # "card" | "qr"
    ref: str  # rrn / SBP operation id — stable for de-duplication
    at: datetime  # Moscow time
    amount: int  # kopecks, positive
    refund: bool
    store: str | None  # retail_store_id when known (card terminal), None for a shared SBP terminal
    label: str = ""  # masked card number or SBP terminal name


@dataclass(frozen=True)
class Receipt:
    store: str  # retail_store_id
    kind: str  # "sale" | "return"
    name: str
    at: datetime  # Moscow time
    cash: int  # kopecks
    card: int
    qr: int
    url: str = ""

    @property
    def total(self) -> int:
        return self.cash + self.card + self.qr

    def part(self, channel: str) -> int:
        return self.card if channel == "card" else self.qr


@dataclass(frozen=True)
class Finding:
    code: str
    store: str  # retail_store_id ("" when unknown)
    message: str
    ref: str  # stable id for de-duplication
    receipt_url: str = ""
    amount: int = 0  # kopecks the finding is about, when it is about one amount


@dataclass
class DayResult:
    matched: list[tuple[Payment, tuple[Receipt, ...]]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # store -> channel -> net amount of the payments assigned to that store
    assigned: dict[str, dict[str, int]] = field(default_factory=dict)


def rub(kopecks: int) -> str:
    sign = "-" if kopecks < 0 else ""
    value = abs(kopecks)
    return f"{sign}{value // 100:,}".replace(",", " ") + f",{value % 100:02d} ₽"


def _hhmm(value: datetime) -> str:
    return value.strftime("%H:%M")


def _utc_to_moscow(raw: str) -> datetime:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(MOSCOW)


def parse_card_operation(row: dict[str, Any], store: str) -> Payment:
    return Payment(
        channel="card",
        ref=f"rrn:{row.get('rrn') or ''}:{row.get('transactionDate')}",
        at=_utc_to_moscow(row["transactionDate"]),
        amount=abs(int(row.get("amount") or 0)),
        refund=str(row.get("type") or "").lower() == "credit",
        store=store,
        label=str(row.get("cardNumber") or ""),
    )


def parse_receipt(store: str, kind: str, row: dict[str, Any]) -> Receipt:
    """MoySklad retaildemand/retailsalesreturn; moments are in the account's (Moscow) time."""
    moment = datetime.strptime(str(row["moment"]).split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MOSCOW)
    return Receipt(
        store=store,
        kind=kind,
        name=str(row.get("name") or ""),
        at=moment,
        cash=abs(round(row.get("cashSum") or 0)),
        card=abs(round(row.get("noCashSum") or 0)),
        qr=abs(round(row.get("qrSum") or 0)),
        url=str((row.get("meta") or {}).get("uuidHref") or ""),
    )


class _Matcher:
    def __init__(self, payments: list[Payment], receipts: list[Receipt], window: timedelta) -> None:
        self.window = window
        self.payments = sorted(payments, key=lambda p: p.at)
        self.receipts = receipts
        self.open_payments: list[Payment] = list(self.payments)
        # receipt index -> channel -> still unmatched amount (all-or-nothing)
        self.open_parts: dict[int, dict[str, int]] = {i: {"card": r.card, "qr": r.qr} for i, r in enumerate(receipts)}
        self.result = DayResult()

    def eligible(self, p: Payment, i: int) -> bool:
        r = self.receipts[i]
        return r.kind == ("return" if p.refund else "sale") and (p.store is None or p.store == r.store)

    def assign(self, p: Payment, store: str) -> None:
        sign = -1 if p.refund else 1
        self.result.assigned.setdefault(store, {"card": 0, "qr": 0})[p.channel] += sign * p.amount

    def close(self, p: Payment, indexes: tuple[int, ...]) -> None:
        self.open_payments.remove(p)
        for i in indexes:
            self.open_parts[i][p.channel] = 0
        self.assign(p, self.receipts[indexes[0]].store)
        self.result.matched.append((p, tuple(self.receipts[i] for i in indexes)))

    def single_pass(self, window: timedelta | None) -> None:
        for p in list(self.open_payments):
            best = None
            for i, r in enumerate(self.receipts):
                if not self.eligible(p, i) or self.open_parts[i][p.channel] != p.amount:
                    continue
                gap = abs(r.at - p.at)
                if window is not None and gap > window:
                    continue
                if best is None or gap < abs(self.receipts[best].at - p.at):
                    best = i
            if best is not None:
                self.close(p, (best,))

    def split_pass(self) -> None:
        # one payment covering several receipts of one store
        for p in list(self.open_payments):
            near = [i for i, r in enumerate(self.receipts)
                    if self.eligible(p, i) and self.open_parts[i][p.channel] > 0 and abs(r.at - p.at) <= SPLIT_WINDOW]
            done = False
            for size in range(2, MAX_SPLIT + 1):
                for combo in itertools.combinations(near, size):
                    if len({self.receipts[i].store for i in combo}) == 1 and sum(self.open_parts[i][p.channel] for i in combo) == p.amount:
                        self.close(p, combo)
                        done = True
                        break
                if done:
                    break
        # one receipt paid by several payments of the same channel
        for i, r in enumerate(self.receipts):
            for channel in ("card", "qr"):
                need = self.open_parts[i][channel]
                if need <= 0:
                    continue
                near = [p for p in self.open_payments if p.channel == channel and self.eligible(p, i) and abs(r.at - p.at) <= SPLIT_WINDOW]
                for size in range(2, MAX_SPLIT + 1):
                    combo = next((c for c in itertools.combinations(near, size) if sum(p.amount for p in c) == need), None)
                    if combo:
                        for p in combo:
                            self.open_payments.remove(p)
                            self.assign(p, r.store)
                            self.result.matched.append((p, (r,)))
                        self.open_parts[i][channel] = 0
                        break

    def cancelled_pairs(self) -> None:
        """A sale and a return of the same receipt amount minutes apart with no
        payment behind either is a receipt punched and immediately cancelled."""
        for channel in ("card", "qr"):
            sales = [i for i, r in enumerate(self.receipts) if r.kind == "sale" and self.open_parts[i][channel] > 0]
            returns = [i for i, r in enumerate(self.receipts) if r.kind == "return" and self.open_parts[i][channel] > 0]
            for s in sales:
                for j in returns:
                    a, b = self.receipts[s], self.receipts[j]
                    if a.store == b.store and self.open_parts[s][channel] == self.open_parts[j][channel] and abs(a.at - b.at) <= self.window:
                        self.open_parts[s][channel] = self.open_parts[j][channel] = 0
                        returns.remove(j)
                        break

    def wrong_type(self) -> None:
        for p in list(self.open_payments):
            best = None
            for i, r in enumerate(self.receipts):
                if not self.eligible(p, i) or r.part(p.channel) != 0 or r.total != p.amount or abs(r.at - p.at) > self.window:
                    continue
                other = "card" if p.channel == "qr" else "qr"
                if r.part(other) and self.open_parts[i][other] == 0:
                    continue  # that receipt's own payment was found — not this one
                if best is None or abs(r.at - p.at) < abs(self.receipts[best].at - p.at):
                    best = i
            if best is None:
                continue
            r = self.receipts[best]
            self.open_payments.remove(p)
            self.open_parts[best] = {"card": 0, "qr": 0}
            self.assign(p, r.store)
            paid_as = "картой" if r.card else ("по СБП (QR)" if r.qr else "наличными")
            doc = "Возврат" if r.kind == "return" else "Чек"
            what = "возврат" if p.refund else "оплата"
            label = f" {p.label}" if p.channel == "card" and p.label else ""
            self.result.findings.append(Finding(
                code=f"{p.channel}_as_{'card' if r.card else ('qr' if r.qr else 'cash')}",
                store=r.store,
                message=f"{doc} {r.name} ({_hhmm(r.at)}, {rub(r.total)}) проведён {paid_as}, "
                        f"а в {_hhmm(p.at)} прошла {what} {CHANNEL_WORDS[p.channel]}{label} на эту сумму",
                ref=p.ref,
                receipt_url=r.url,
            ))

    def report_rest(self) -> None:
        stores_with_receipts = {r.store for r in self.receipts}
        # A store whose terminal took payments while MoySklad has no receipt
        # at all that day: the till didn't upload (or nothing was punched) —
        # one finding for the whole day instead of one per payment.
        silent: dict[str, list[Payment]] = {}
        for p in self.open_payments:
            if p.store and p.store not in stores_with_receipts and not p.refund:
                silent.setdefault(p.store, []).append(p)
        for store, payments in silent.items():
            total = sum(p.amount for p in payments)
            self.result.findings.append(Finding(
                "no_receipts", store,
                f"По терминалу {len(payments)} оплат {CHANNEL_WORDS['card']} на {rub(total)} "
                f"({_hhmm(payments[0].at)}–{_hhmm(payments[-1].at)}), а чеков за день в МойСклад нет совсем — касса не выгрузила продажи или их не пробили",
                "no_receipts",
            ))
            for p in payments:
                self.open_payments.remove(p)
                self.assign(p, store)
        for p in self.open_payments:
            what = "возврат" if p.refund else "оплата"
            label = f" {p.label}" if p.label else ""
            doc = "возврата" if p.refund else "чека"
            if p.store:
                self.assign(p, p.store)
            self.result.findings.append(Finding(
                f"{p.channel}_without_receipt", p.store or "",
                f"В {_hhmm(p.at)} прошла {what} {CHANNEL_WORDS[p.channel]}{label} на {rub(p.amount)}, а {doc} на эту сумму в МойСклад нет",
                p.ref,
                amount=p.amount,
            ))
        for i, r in enumerate(self.receipts):
            doc = "Возврат" if r.kind == "return" else "Чек"
            for channel in ("card", "qr"):
                if self.open_parts[i][channel] > 0:
                    self.result.findings.append(Finding(
                        f"receipt_without_{channel}", r.store,
                        f"{doc} {r.name} ({_hhmm(r.at)}) проведён {CHANNEL_WORDS[channel]} на {rub(self.open_parts[i][channel])}, "
                        f"а {'возврата' if r.kind == 'return' else 'оплаты'} {CHANNEL_WORDS[channel]} на эту сумму нет — возможно, оплатили иначе",
                        f"{r.kind}:{r.name}:{channel}",
                        r.url,
                        amount=self.open_parts[i][channel] * (-1 if r.kind == "return" else 1),
                    ))


def reconcile_day(payments: list[Payment], receipts: list[Receipt], *, window: timedelta = MATCH_WINDOW,
                  card_stores: set[str] | None = None) -> DayResult:
    """Match one day's payments (all channels, all stores) to its receipts.

    `card_stores` limits "receipt without card payment" findings to stores
    whose terminal operations were actually fetched — a store without a
    mapped terminal must not have every card receipt flagged."""
    m = _Matcher(payments, receipts, window)
    m.single_pass(window)
    m.split_pass()
    m.single_pass(None)
    m.cancelled_pairs()
    m.wrong_type()
    m.report_rest()
    if card_stores is not None:
        m.result.findings = [f for f in m.result.findings if not (f.code == "receipt_without_card" and f.store not in card_stores)]
    return m.result


def absorb_phone_payments(result: DayResult, store: str, residual: int) -> int:
    """Card payments taken on a phone (T-Bank SoftPOS) are paid out with the
    terminals but are invisible to T-API — confirmed live for ТЦ Мега Химки.
    `residual` = the day's card payout minus every terminal's payments. The
    store's card receipts left without a terminal operation whose amounts
    add up to exactly that residual are those phone payments, not errors.
    Only an explained residual is credited to the store — unexplained money
    may just as well be another store's phone payment with no receipt.
    Returns the part no receipt accounts for (0 when fully explained)."""
    if residual <= 0:
        return residual
    candidates = [f for f in result.findings if f.code == "receipt_without_card" and f.store == store and f.amount > 0][:16]
    best: tuple[Finding, ...] | None = None
    for size in range(len(candidates), 0, -1):
        best = next((c for c in itertools.combinations(candidates, size) if sum(f.amount for f in c) == residual), None)
        if best:
            break
    if not best:
        return residual
    result.findings = [f for f in result.findings if f not in best]
    result.assigned.setdefault(store, {"card": 0, "qr": 0})["card"] += residual
    return 0


# --- statement ------------------------------------------------------------

TBANK_INN = "7710140679"
_CARD_PAYOUT_RE = re.compile(r"Зачисление средств по терминалам эквайринга от (\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)
_CARD_FEE_RE = re.compile(r"Комиссия за операции по терминалам эквайринга от (\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)
_CARD_REFUND_RE = re.compile(r"Возврат средств по терминалам эквайринга от (\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)
_SBP_RE = re.compile(r"Пополнение по операции СБП (\S+?)\.? Терминал (.+)$", re.IGNORECASE)
_SBP_FEE_RE = re.compile(r"Плата за пополнение по операции СБП (\S+?)\.", re.IGNORECASE)


@dataclass(frozen=True)
class StatementLine:
    kind: str  # "card_payout" | "card_fee" | "card_refund" | "sbp" | "sbp_fee"
    operation_id: str
    booked_at: datetime  # Moscow time
    amount: int  # kopecks, positive
    purpose: str
    payout_day: date | None = None  # card lines: the "от DD.MM.YYYY" date
    sbp_id: str = ""
    terminal: str = ""


def _ru_date(raw: str) -> date:
    return datetime.strptime(raw, "%d.%m.%Y").date()


def parse_statement_line(row: dict[str, Any]) -> StatementLine | None:
    purpose = str(row.get("payPurpose") or row.get("description") or "").strip()
    credit = str(row.get("typeOfOperation") or "") == "Credit"
    when_raw = row.get("authorizationDate") or row.get("operationDate") or row.get("docDate")
    if not when_raw:
        return None
    base = dict(
        operation_id=str(row.get("operationId") or ""),
        booked_at=_utc_to_moscow(str(when_raw)),
        amount=round(float(row.get("accountAmount") or row.get("operationAmount") or 0) * 100),
        purpose=purpose,
    )
    if credit and (m := _CARD_PAYOUT_RE.search(purpose)):
        return StatementLine("card_payout", payout_day=_ru_date(m.group(1)), **base)
    if credit and (m := _SBP_RE.search(purpose)):
        return StatementLine("sbp", sbp_id=m.group(1), terminal=m.group(2).strip(), **base)
    if not credit and (m := _CARD_FEE_RE.search(purpose)):
        return StatementLine("card_fee", payout_day=_ru_date(m.group(1)), **base)
    if not credit and (m := _CARD_REFUND_RE.search(purpose)):
        return StatementLine("card_refund", payout_day=_ru_date(m.group(1)), **base)
    if not credit and (m := _SBP_FEE_RE.search(purpose)):
        return StatementLine("sbp_fee", sbp_id=m.group(1), **base)
    return None


def sbp_payment(line: StatementLine, store: str | None) -> Payment:
    return Payment(channel="qr", ref=f"sbp:{line.sbp_id}", at=line.booked_at, amount=line.amount, refund=False, store=store, label=line.terminal)
