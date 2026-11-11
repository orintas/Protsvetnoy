from datetime import datetime

from sync_service.acquiring_reconcile import (
    MOSCOW,
    Payment,
    Receipt,
    absorb_phone_payments,
    parse_card_operation,
    parse_receipt,
    parse_statement_line,
    reconcile_day,
    rub,
)

A = "store-a"
B = "store-b"


def at(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 11, 7, int(h), int(m), tzinfo=MOSCOW)


def card(hhmm, amount, store=A, refund=False, rrn="1"):
    return Payment("card", f"rrn:{rrn}:{hhmm}", at(hhmm), amount, refund, store, "220220******0915")


def qr(hhmm, amount, store=None, ref="9527040784"):
    return Payment("qr", f"sbp:{ref}", at(hhmm), amount, False, store, "tsvetnoy.shop")


def receipt(hhmm, name, *, cash=0, card_=0, qr_=0, store=A, kind="sale"):
    return Receipt(store, kind, name, at(hhmm), cash, card_, qr_, f"https://ms/{name}")


def codes(result):
    return sorted(f.code for f in result.findings)


def test_exact_card_and_sbp_matches_have_no_findings():
    result = reconcile_day(
        [card("12:00", 529000), qr("15:00", 417000)],
        [receipt("12:01", "1", card_=529000), receipt("15:01", "2", qr_=417000, store=B)],
    )
    assert codes(result) == []
    assert result.assigned == {A: {"card": 529000, "qr": 0}, B: {"card": 0, "qr": 417000}}


def test_card_payment_on_a_cash_receipt_is_reported_as_wrong_type():
    result = reconcile_day([card("12:00", 350000)], [receipt("12:03", "19415", cash=350000)])
    assert codes(result) == ["card_as_cash"]
    assert "19415" in result.findings[0].message and "наличными" in result.findings[0].message
    assert result.findings[0].receipt_url == "https://ms/19415"


def test_sbp_payment_on_a_card_receipt_is_reported_once():
    # Confirmed live: ТЦ Мега Химки 29.10, чек 19419 на 200 ₽ — картой в чеке, СБП в банке.
    result = reconcile_day([qr("17:06", 20000)], [receipt("17:06", "19419", card_=20000)], card_stores={A})
    assert codes(result) == ["qr_as_card"]


def test_one_payment_for_two_receipts_and_one_receipt_paid_by_two_cards():
    result = reconcile_day(
        [card("17:59", 1000000), card("14:47", 709000, rrn="2"), card("14:48", 219000, rrn="3")],
        [receipt("18:03", "09409", card_=599000), receipt("18:04", "09410", card_=401000),
         receipt("15:40", "09807", card_=928000)],
    )
    assert codes(result) == []


def test_sale_cancelled_by_an_immediate_return_is_ignored():
    result = reconcile_day([], [receipt("14:05", "09400", card_=549000), receipt("14:06", "00118", card_=549000, kind="return")], card_stores={A})
    assert codes(result) == []


def test_a_day_without_any_receipt_is_one_finding():
    result = reconcile_day([card("14:15", 450000), card("20:14", 398000, rrn="2")], [], card_stores={A})
    assert codes(result) == ["no_receipts"]
    assert "2 оплат" in result.findings[0].message
    assert result.assigned[A]["card"] == 848000


def test_card_receipt_without_payment_only_for_stores_with_a_terminal():
    receipts = [receipt("12:00", "1", card_=100000)]
    assert codes(reconcile_day([], receipts, card_stores={A})) == ["receipt_without_card"]
    assert codes(reconcile_day([], receipts, card_stores=set())) == []


def test_sbp_payment_without_receipt():
    result = reconcile_day([qr("12:51", 355000)], [receipt("15:08", "1", card_=100000)], card_stores=set())
    assert codes(result) == ["qr_without_receipt"]


def test_card_refund_matches_a_return():
    result = reconcile_day([card("11:34", 411600, refund=True)], [receipt("11:35", "R1", card_=411600, kind="return")])
    assert codes(result) == []
    assert result.assigned[A]["card"] == -411600


def test_phone_payments_explain_card_receipts_without_terminal_operation():
    result = reconcile_day([], [receipt("14:52", "19437", card_=756000), receipt("21:20", "19436", card_=657400)], card_stores={A})
    assert absorb_phone_payments(result, A, 1413400) == 0
    assert codes(result) == []
    assert result.assigned[A]["card"] == 1413400


def test_unexplained_phone_residual_is_not_credited_to_the_store():
    result = reconcile_day([], [receipt("14:52", "19437", card_=756000)], card_stores={A})
    assert absorb_phone_payments(result, A, 549000) == 549000
    assert codes(result) == ["receipt_without_card"]
    assert A not in result.assigned


def test_parse_card_operation_converts_utc_to_moscow():
    payment = parse_card_operation({"rrn": "630015024171", "transactionDate": "2026-10-27T15:02:58Z", "amount": 529000,
                                    "cardNumber": "220220******0915", "type": "Debit"}, A)
    assert payment.at.hour == 18 and payment.amount == 529000 and not payment.refund


def test_parse_receipt_keeps_kopecks():
    r = parse_receipt(A, "sale", {"name": "19490", "moment": "2026-11-11 15:10:00.000", "cashSum": 1054000.0, "noCashSum": 0.0, "qrSum": 0.0})
    assert (r.cash, r.card, r.qr, r.at.hour) == (1054000, 0, 0, 15)


def _line(purpose, amount, credit=True, when="2026-10-28T15:35:13Z"):
    return {"operationId": "op", "typeOfOperation": "Credit" if credit else "Debit", "accountAmount": amount,
            "payPurpose": purpose, "operationDate": when, "authorizationDate": when}


def test_statement_lines_as_seen_live():
    payout = parse_statement_line(_line("Зачисление средств по терминалам эквайринга от 28.10.2026.", 57250.0))
    assert payout.kind == "card_payout" and payout.payout_day.isoformat() == "2026-10-28" and payout.amount == 5725000
    fee = parse_statement_line(_line("Комиссия за операции по терминалам эквайринга от 28.10.2026., в т.ч. НДС 22% 133 руб. 15 коп. по договору  № 7037497588", 974.73, credit=False))
    assert fee.kind == "card_fee" and fee.amount == 97473
    sbp = parse_statement_line(_line("Пополнение по операции СБП 2076290666. Терминал MEGA KHIMKI SHOPPING C", 200.0))
    assert (sbp.kind, sbp.sbp_id, sbp.terminal, sbp.booked_at.hour) == ("sbp", "2076290666", "MEGA KHIMKI SHOPPING C", 18)
    sbp_fee = parse_statement_line(_line("Плата за пополнение по операции СБП 9527040784. Терминал tsvetnoy.shop. Договор 7037497588", 38.43, credit=False))
    assert (sbp_fee.kind, sbp_fee.sbp_id) == ("sbp_fee", "9527040784")
    refund = parse_statement_line(_line("Возврат средств по терминалам эквайринга от 04.11.2026", 4116.0, credit=False))
    assert refund.kind == "card_refund"
    assert parse_statement_line(_line("Оплата по договору 67227304/26. По поручению ООО Яндекс Маркет", 4451.93)) is None


def test_rub_formatting():
    assert rub(5725000) == "57 250,00 ₽"
    assert rub(-411600) == "-4 116,00 ₽"
