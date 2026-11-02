import csv
import sqlite3

import pytest

from app.db import connect_writable
from app.ingest.statements import StatementError, classify, load, parse_paise, parse_statement

HEADER = "date,account,narration,debit,credit\n"


def write_csv(path, *rows):
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("raw, expected", [("1,234.50", 123450), ("", 0), ("0.07", 7), (" 100 ", 10000), ("1,00,000.00", 10000000)])
def test_parse_paise(raw, expected):
    assert parse_paise(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "-5.00", "1.005"])
def test_parse_paise_rejects_bad_values(raw):
    with pytest.raises(StatementError):
        parse_paise(raw)


@pytest.mark.parametrize("narration, direction, expected", [
    ("UPI/412345678901/SWIGGY LTD/swiggy@axisbank/Food", "debit", ("Swiggy", "purchase", "Food Delivery")),
    ("UPI/412345678901/SWIGGY INSTAMART/x@axis/Order", "debit", ("Instamart", "purchase", "Groceries")),
    ("POS/412345678901/AMAZON SELLER SERVICES", "debit", ("Amazon", "purchase", "Shopping")),
    ("REFUND/412345678901/AMAZON SELLER SERVICES", "credit", ("Amazon", "refund", "Shopping")),
    ("UPI/REV/412345678901/ZOMATO LIMITED", "credit", ("Zomato", "refund", "Food Delivery")),
    ("UPI/412345678901/CC PAYMENT ICICI CARD/icici@upi/Bill", "debit", ("Credit Card Payment", "card_payment", "Transfers")),
    ("ACH/HDFCMF SIP ELSS TAX SAVER/123", "debit", ("Hdfcmf Sip Elss Tax Saver", "investment", "Investments")),
    ("NEFT/N123/SALARY/ACME TECHNOLOGIES PVT LTD", "credit", ("Acme Technologies", "income", "Salary")),
    ("ATM/123/MG ROAD", "debit", ("ATM", "cash_withdrawal", "Cash")),
    ("UPI/412345678901/RAHUL VERMA/rahul@oksbi/Split", "debit", ("Rahul Verma", "p2p", "People")),
    ("UPI/412345678901/SHARMA KIRANA STORE/sharma@okaxis/Pay", "debit", ("Sharma Kirana Store", "purchase", "Other")),
])
def test_classify(narration, direction, expected):
    assert classify(narration, direction) == expected


def test_keyword_does_not_match_inside_a_word():
    # "OLA" must not match "KOLAR", or a person would be filed as the cab company.
    merchant, txn_type, _ = classify("UPI/1/KOLAR REDDY/k@oksbi/Pay", "debit")
    assert (merchant, txn_type) == ("Kolar Reddy", "p2p")


def test_self_name_marks_internal_transfer():
    assert classify("UPI/1/ARJUN MEHTA/a@oksbi/Self", "debit", frozenset({"Arjun Mehta"}))[1] == "internal_transfer"


def test_transfer_pair_across_accounts_is_internal(tmp_path):
    path = write_csv(tmp_path / "s.csv",
                     "15/09/2026,A,IMPS/1/TRANSFER/ME,1000.00,",
                     "15/09/2026,B,IMPS/1/TRANSFER/ME,,1000.00")
    assert {t.txn_type for t in parse_statement(path)} == {"internal_transfer"}


def test_same_amount_upi_is_not_treated_as_a_transfer(tmp_path):
    path = write_csv(tmp_path / "s.csv",
                     "15/09/2026,A,UPI/1/RAHUL VERMA/r@oksbi/Pay,500.00,",
                     "15/09/2026,B,UPI/2/PRIYA NAIR/p@oksbi/Pay,,500.00")
    assert {t.txn_type for t in parse_statement(path)} == {"p2p"}


def test_reimporting_and_overlapping_statements_add_nothing_new(tmp_path):
    first = write_csv(tmp_path / "a.csv", "01/09/2026,A,UPI/1/SWIGGY LTD/s@a/F,100.00,", "02/09/2026,A,UPI/2/UBER/u@a/R,50.00,")
    second = write_csv(tmp_path / "b.csv", "02/09/2026,A,UPI/2/UBER/u@a/R,50.00,", "03/09/2026,A,UPI/3/OLA/o@a/R,70.00,")
    conn = connect_writable(tmp_path / "t.db")
    assert load(conn, parse_statement(first)) == 2
    assert load(conn, parse_statement(first)) == 0
    assert load(conn, parse_statement(second)) == 1


def test_identical_rows_in_one_statement_are_both_kept(tmp_path):
    path = write_csv(tmp_path / "s.csv", "01/09/2026,A,CARD/TEA STALL,20.00,", "01/09/2026,A,CARD/TEA STALL,20.00,")
    conn = connect_writable(tmp_path / "t.db")
    assert load(conn, parse_statement(path)) == 2


@pytest.mark.parametrize("row", ["31/02/2026,A,X,1.00,", "01/09/2026,A,X,,", "01/09/2026,A,X,1.00,1.00", "01/09/2026,A,X,abc,"])
def test_bad_rows_raise_with_location(tmp_path, row):
    with pytest.raises(StatementError, match=r"s\.csv:2"):
        parse_statement(write_csv(tmp_path / "s.csv", row))


def truth_rows(statement):
    with open(statement[1], newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_every_row_matches_generator_truth(statement, db_path):
    conn = sqlite3.connect(db_path)
    stored = {(n, a, d): (t, c) for n, a, d, t, c in conn.execute(
        "SELECT narration, account, txn_date, txn_type, category FROM transactions")}
    truth = truth_rows(statement)
    wrong = [r["narration"] for r in truth if stored.get((r["narration"], r["account"], r["date"])) != (r["txn_type"], r["category"])]
    assert not wrong, wrong[:5]
    assert len(stored) == len(truth)


def test_spending_view_total_matches_truth_computed_independently(statement, db_path):
    """Sums the truth file directly in Python, sharing no SQL with the view it checks."""
    expected = 0
    for row in truth_rows(statement):
        if row["txn_type"] in ("purchase", "p2p", "fee", "cash_withdrawal"):
            expected += int(row["debit_paise"])
        elif row["txn_type"] == "refund":
            expected -= int(row["credit_paise"])
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT SUM(net_amount_paise) FROM spending").fetchone()[0] == expected


def test_spending_excludes_what_would_double_count(db_path):
    conn = sqlite3.connect(db_path)
    types = {row[0] for row in conn.execute("SELECT DISTINCT txn_type FROM spending")}
    assert types.isdisjoint({"internal_transfer", "card_payment", "investment", "income"})
    naive = conn.execute("SELECT SUM(amount_paise) FROM transactions WHERE direction = 'debit'").fetchone()[0]
    assert conn.execute("SELECT SUM(net_amount_paise) FROM spending").fetchone()[0] < naive
