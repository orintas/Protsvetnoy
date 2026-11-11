from datetime import date, datetime

from sync_service.acquiring_reconcile import MOSCOW
from sync_service.acquiring_sync import AcquiringAccount, AcquiringConfig, AcquiringLog, AcquiringStore, Run
from sync_service.error_log import ErrorLog

MAIN, HATS = "main", "hats"
MEGA, HATS_STORE = "mega-id", "hats-id"


def _account(key):
    return AcquiringAccount(
        key=key, name=f"МС {key}", token_env="X", organization_id=f"org-{key}", organization_account_id=f"acc-{key}",
        group_id="g", card_agent={"type": "counterparty", "id": f"tbank-{key}"}, sbp_agent={"type": "organization", "id": f"org-{key}"},
        fee_agent={"type": "counterparty", "id": f"fee-{key}"}, fee_expense_item_id="bank",
    )


CONFIG = AcquiringConfig(
    account_number="40702810110000874313", start_day=date(2026, 11, 1), phone_payments_store=MEGA, sbp_terminals={},
    accounts=(_account(MAIN), _account(HATS)),
    stores=(AcquiringStore("ТЦ Мега Химки", MEGA, "T1", MAIN), AcquiringStore("Шапки", HATS_STORE, "T2", HATS)),
)


class FakeMoySklad:
    base_url = "https://ms"

    def __init__(self, receipts, *, payments_in=None, shifts=True):
        self.receipts = receipts  # store id -> list of retaildemand rows
        self.payments_in = payments_in or []
        self.shifts = shifts
        self.created_in: list[dict] = []
        self.created_out: list[dict] = []

    def documents(self, entity, *, document_filter, limit=1000):
        if entity == "retaildemand":
            return [r for store, rows in self.receipts.items() if f"retailstore/{store};" in document_filter for r in rows]
        if entity == "paymentin":
            return list(self.payments_in)
        return []

    def retail_shift_for_day(self, store_id, day):
        return {"id": f"shift-{store_id}-{day}", "name": "00001"} if self.shifts else None

    def create_payment_in(self, **kwargs):
        self.created_in.append(kwargs)
        return {"name": str(len(self.created_in)), "meta": {"uuidHref": "https://ms/pin"}}

    def create_payment_out(self, **kwargs):
        self.created_out.append(kwargs)
        return {"name": "fee", "meta": {"uuidHref": "https://ms/pout"}}


class FakeTBank:
    def __init__(self, ops, statement):
        self.ops = ops
        self._statement = statement

    def terminals(self):
        return [{"key": "T1", "id": "1"}, {"key": "T2", "id": "2"}]

    def terminal_operations(self, key, start, end):
        return [o for o in self.ops.get(key, [])
                if start <= datetime.fromisoformat(o["transactionDate"].replace("Z", "+00:00")) < end]

    def statement(self, account, start, end):
        return self._statement


def _op(when_utc, amount, rrn):
    return {"rrn": rrn, "transactionDate": when_utc, "amount": amount, "cardNumber": "2200******0001", "type": "Debit"}


def _rd(name, moment, *, card=0, cash=0, qr=0):
    return {"name": name, "moment": moment, "cashSum": cash, "noCashSum": card, "qrSum": qr, "meta": {"uuidHref": f"https://ms/{name}"}}


def _st(purpose, amount, credit=True, when="2026-11-02T22:00:00Z", op="op"):
    return {"operationId": op, "typeOfOperation": "Credit" if credit else "Debit", "accountAmount": amount, "payPurpose": purpose,
            "operationDate": when, "authorizationDate": when}


OPS = {"T1": [_op("2026-11-02T09:00:00Z", 600000, "a")], "T2": [_op("2026-11-02T10:00:00Z", 400000, "b")]}
STATEMENT = [
    # Mega's phone payment of 1 000 ₽ is in the payout but not in T-API.
    _st("Зачисление средств по терминалам эквайринга от 03.11.2026.", 11000.0, op="payout"),
    _st("Комиссия за операции по терминалам эквайринга от 03.11.2026., в т.ч. НДС", 196.90, credit=False, op="fee"),
    _st("Пополнение по операции СБП 111. Терминал tsvetnoy.shop", 500.0, when="2026-11-02T12:00:00Z", op="sbp"),
    _st("Плата за пополнение по операции СБП 111. Терминал tsvetnoy.shop. Договор 1", 3.5, credit=False, when="2026-11-02T12:01:00Z", op="sbpfee"),
]


def _clients(**main_kwargs):
    main = FakeMoySklad({MEGA: [_rd("1", "2026-11-02 12:00:00.000", card=600000), _rd("2", "2026-11-02 15:00:00.000", qr=50000),
                                _rd("3", "2026-11-02 18:00:00.000", card=100000)]}, **main_kwargs)
    hats = FakeMoySklad({HATS_STORE: [_rd("H1", "2026-11-02 13:01:00.000", card=400000)]})
    return {MAIN: main, HATS: hats}


def _run(tmp_path, clients, *, dry_run):
    log = AcquiringLog(str(tmp_path / "acq.sqlite3"))
    errors = ErrorLog(str(tmp_path / "err.sqlite3"))
    run = Run(config=CONFIG, moysklad=clients, tbank=FakeTBank(OPS, STATEMENT), log=log, errors=errors,
              dry_run=dry_run, now=datetime(2026, 11, 3, 10, 0, tzinfo=MOSCOW))
    return run, log, errors


def test_dry_run_writes_nothing_but_logs_the_plan(tmp_path):
    clients = _clients()
    run, log, errors = _run(tmp_path, clients, dry_run=True)
    run.execute()
    assert clients[MAIN].created_in == [] and clients[MAIN].created_out == [] and clients[HATS].created_in == []
    messages = [e["message"] for e in log.recent()]
    assert any("был бы создан входящий платёж по картам на 7 000,00 ₽" in m for m in messages)  # 6 000 terminal + 1 000 phone
    assert errors.recent() == []


def test_live_run_books_gross_payments_and_fee_shares(tmp_path):
    clients = _clients()
    run, log, errors = _run(tmp_path, clients, dry_run=False)
    run.execute()
    main, hats = clients[MAIN], clients[HATS]
    by_channel = {(p["agent_id"], p["amount"]) for p in main.created_in}
    assert by_channel == {("tbank-main", 700000), ("org-main", 50000)}
    assert [(p["agent_id"], p["amount"]) for p in hats.created_in] == [("tbank-hats", 400000)]
    assert all(p["retail_shift_id"].startswith("shift-") for p in main.created_in)
    # card fee 196,90 split 7 000 : 4 000, SBP fee 3,50 goes to the store that took the payment
    assert main.created_out[0]["amount"] == round(19690 * 7 / 11) + 350
    assert hats.created_out[0]["amount"] == round(19690 * 4 / 11)
    assert errors.recent() == []
    # the day is done — a second run books nothing again
    run.execute()
    assert len(main.created_in) == 2 and len(main.created_out) == 1


def test_manual_payment_on_the_shift_is_respected(tmp_path):
    manual = {"name": "030090615", "externalCode": "x", "agent": {"meta": {"href": "https://ms/counterparty/tbank-main"}},
              "operations": [{"meta": {"href": "https://ms/retailshift/shift-mega-id-2026-11-02", "type": "retailshift"}}]}
    clients = _clients(payments_in=[manual])
    run, log, _ = _run(tmp_path, clients, dry_run=False)
    run.execute()
    assert [p["agent_id"] for p in clients[MAIN].created_in] == ["org-main"]  # only SBP; card was done by hand
    assert any(e["kind"] == "payment_manual" for e in log.recent())


def test_missing_shift_is_an_error_not_a_payment(tmp_path):
    clients = _clients(shifts=False)
    run, log, errors = _run(tmp_path, clients, dry_run=False)
    run.execute()
    assert clients[MAIN].created_in == []
    assert any("смены за этот день в МойСклад нет" in e["message"] for e in errors.recent())


def test_day_waits_for_the_card_payout(tmp_path):
    clients = _clients()
    log = AcquiringLog(str(tmp_path / "acq.sqlite3"))
    run = Run(config=CONFIG, moysklad=clients, tbank=FakeTBank(OPS, STATEMENT[2:]), log=log, errors=ErrorLog(str(tmp_path / "e.sqlite3")),
              dry_run=False, now=datetime(2026, 11, 3, 10, 0, tzinfo=MOSCOW))
    assert run.execute() == 0
    assert clients[MAIN].created_in == []
