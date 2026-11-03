"""Parse statement CSVs into the transactions table.

Expected columns: date (DD/MM/YYYY), account, narration, debit, credit.
Run: python -m app.ingest.statements data/sample/transactions.csv --db data/paisalens.db
"""
import argparse
import csv
import hashlib
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.db import connect_writable
from app.ingest.categorise import clean_name, looks_like_shop, lookup_merchant

CARD_PAYMENT_MARKERS = ("CC PAYMENT", "CREDIT CARD PAYMENT", "CARD BILL", "PAYMENT RECEIVED")
TRANSFER_RAILS = {"NEFT", "IMPS", "RTGS"}
INTERNAL_TRANSFER_WINDOW_DAYS = 1
UPI_NON_NAMES = {"DR", "CR", "P2A", "P2M", "UPI", "PAY", "PAYMENT"}
IFSC_PATTERN = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


class StatementError(ValueError):
    pass


@dataclass
class Transaction:
    account: str
    txn_date: date
    narration: str
    merchant: str
    amount_paise: int
    direction: str
    txn_type: str
    category: str
    occurrence: int = 0

    @property
    def rail(self) -> str:
        return self.narration.split("/", 1)[0].strip().upper()

    @property
    def txn_hash(self) -> str:
        # `occurrence` separates genuine same-day repeats within a statement,
        # while re-importing that statement still reproduces the same hashes.
        key = "|".join([self.account, self.txn_date.isoformat(), self.narration,
                        str(self.amount_paise), self.direction, str(self.occurrence)])
        return hashlib.sha256(key.encode()).hexdigest()


def parse_paise(raw: str) -> int:
    """'1,234.50' -> 123450. Blank means 0. Uses Decimal so no float rounding reaches the database."""
    text = raw.replace(",", "").strip()
    if not text:
        return 0
    try:
        value = Decimal(text) * 100
    except InvalidOperation as exc:
        raise StatementError(f"not an amount: {raw!r}") from exc
    if value < 0 or value != value.to_integral_value():
        raise StatementError(f"not a valid amount: {raw!r}")
    return int(value)


def classify(narration: str, direction: str, self_names: frozenset[str] = frozenset()) -> tuple[str, str, str]:
    """Return (merchant, txn_type, category) for one narration."""
    upper = narration.upper()
    if upper.startswith("UPI-"):  # some banks separate fields with hyphens
        narration = narration.replace("-", "/")
    parts = [part.strip() for part in narration.split("/")]
    rail = parts[0].upper()

    if any(marker in upper for marker in CARD_PAYMENT_MARKERS):
        return "Credit Card Payment", "card_payment", "Transfers"
    if any(name and name.upper() in upper for name in self_names):
        return "Own Account", "internal_transfer", "Transfers"
    if rail == "ATM":
        return "ATM", "cash_withdrawal", "Cash"
    if rail == "CHARGE":
        return "Bank Charges", "fee", "Fees"
    if rail == "INTEREST":
        return "Interest", "income", "Interest"
    if direction == "credit" and "SALARY" in upper:
        return clean_name(parts[-1]), "income", "Salary"
    if rail.startswith(("ACH", "NACH")) and ("SIP" in upper or "MF" in upper or "MUTUAL" in upper):
        return clean_name(parts[1] if len(parts) > 1 else "Mutual Fund"), "investment", "Investments"

    if rail == "REFUND" or (rail == "UPI" and len(parts) > 1 and parts[1].upper() == "REV"):
        payee = parts[-1] if rail == "REFUND" else (parts[3] if len(parts) > 3 else "")
        known = lookup_merchant(payee)
        merchant, category = known or (clean_name(payee), "Other")
        return merchant, "refund", category

    if rail == "UPI":
        payee = _upi_payee(parts)
    elif rail in ("POS", "CARD", "ACH", "NEFT", "IMPS", "RTGS"):
        payee = parts[-1] if rail in TRANSFER_RAILS else (parts[2] if rail == "POS" and len(parts) > 2 else parts[1] if len(parts) > 1 else "")
    else:
        payee = narration

    known = lookup_merchant(payee) or lookup_merchant(narration)
    if known:
        return known[0], "refund" if direction == "credit" else "purchase", known[1]
    if rail in ("UPI", *TRANSFER_RAILS) and not looks_like_shop(payee):
        return clean_name(payee), "p2p", "People"
    if direction == "credit":
        return clean_name(payee), "income", "Other Income"
    return clean_name(payee), "purchase", "Other"


def _upi_payee(parts: list[str]) -> str:
    """The first field that reads like a name. Banks order UPI fields differently."""
    for part in parts[1:]:
        upper = part.upper()
        if "@" in part or not re.search("[A-Z]", upper) or upper in UPI_NON_NAMES or IFSC_PATTERN.match(upper):
            continue
        return part
    return ""


def build_transaction(account: str, txn_date: date, narration: str, debit: int, credit: int,
                      self_names: frozenset[str] = frozenset()) -> Transaction:
    direction = "debit" if debit else "credit"
    merchant, txn_type, category = classify(narration, direction, self_names)
    return Transaction(account, txn_date, narration, merchant, debit or credit, direction, txn_type, category)


def finish_transactions(transactions: list[Transaction]) -> None:
    """Number genuine same-day repeats so they stay distinct, and pair up own-account transfers."""
    seen: Counter[tuple] = Counter()
    for txn in transactions:
        key = (txn.account, txn.txn_date, txn.narration, txn.amount_paise, txn.direction)
        txn.occurrence = seen[key]
        seen[key] += 1
    mark_internal_transfers(transactions)


def relink_internal_transfers(conn: sqlite3.Connection) -> int:
    """Pair own-account transfers across statements uploaded separately. Returns how many pairs were found."""
    placeholders = ", ".join("?" for _ in TRANSFER_RAILS)
    rows = conn.execute(
        f"""SELECT id, account, txn_date, amount_paise, direction FROM transactions
            WHERE txn_type = 'p2p' AND upper(substr(narration, 1, instr(narration || '/', '/') - 1)) IN ({placeholders})""",
        sorted(TRANSFER_RAILS)).fetchall()
    credits = [row for row in rows if row[4] == "credit"]
    pairs = 0
    for debit in (row for row in rows if row[4] == "debit"):
        for credit in credits:
            days = abs((date.fromisoformat(credit[2]) - date.fromisoformat(debit[2])).days)
            if credit[1] != debit[1] and credit[3] == debit[3] and days <= INTERNAL_TRANSFER_WINDOW_DAYS:
                conn.executemany("""UPDATE transactions SET merchant = 'Own Account', txn_type = 'internal_transfer',
                                    category = 'Transfers' WHERE id = ?""", [(debit[0],), (credit[0],)])
                credits.remove(credit)
                pairs += 1
                break
    conn.commit()
    return pairs


def mark_internal_transfers(transactions: list[Transaction]) -> None:
    """Pair bank-rail debits and credits of equal amount across accounts within a day.

    Limited to NEFT/IMPS/RTGS: on UPI, a friend paying you and you paying a shop for the
    same amount on the same day is common enough to create false matches.
    """
    candidates = [t for t in transactions if t.txn_type == "p2p" and t.rail in TRANSFER_RAILS]
    credits = [t for t in candidates if t.direction == "credit"]
    for debit in (t for t in candidates if t.direction == "debit"):
        for credit in credits:
            if (credit.txn_type != "internal_transfer"
                    and credit.account != debit.account
                    and credit.amount_paise == debit.amount_paise
                    and abs((credit.txn_date - debit.txn_date).days) <= INTERNAL_TRANSFER_WINDOW_DAYS):
                for side in (debit, credit):
                    side.merchant, side.txn_type, side.category = "Own Account", "internal_transfer", "Transfers"
                break


def parse_statement(path: str | Path, self_names: frozenset[str] = frozenset()) -> list[Transaction]:
    transactions = []
    seen: Counter[tuple] = Counter()
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            try:
                txn_date = datetime.strptime(row["date"].strip(), "%d/%m/%Y").date()
                debit, credit = parse_paise(row["debit"]), parse_paise(row["credit"])
                if bool(debit) == bool(credit):
                    raise StatementError("exactly one of debit and credit must be set")
            except (KeyError, ValueError) as exc:
                raise StatementError(f"{path}:{line}: {exc}") from exc

            direction = "debit" if debit else "credit"
            narration = row["narration"].strip()
            merchant, txn_type, category = classify(narration, direction, self_names)
            txn = Transaction(row["account"].strip(), txn_date, narration, merchant,
                              debit or credit, direction, txn_type, category)
            key = (txn.account, txn.txn_date, txn.narration, txn.amount_paise, txn.direction)
            txn.occurrence = seen[key]
            seen[key] += 1
            transactions.append(txn)

    mark_internal_transfers(transactions)
    return transactions


def load(conn: sqlite3.Connection, transactions: list[Transaction]) -> int:
    """Insert transactions, skipping any already imported. Returns how many were new."""
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO transactions
           (txn_hash, account, txn_date, narration, merchant, amount_paise, direction, txn_type, category)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(t.txn_hash, t.account, t.txn_date.isoformat(), t.narration, t.merchant,
          t.amount_paise, t.direction, t.txn_type, t.category) for t in transactions],
    )
    added = conn.total_changes - before
    conn.execute("""UPDATE transactions SET category =
                        (SELECT category FROM category_overrides o WHERE o.merchant = transactions.merchant)
                    WHERE merchant IN (SELECT merchant FROM category_overrides)""")
    conn.commit()
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument("--db", default="data/paisalens.db")
    parser.add_argument("--self-name", action="append", default=[],
                        help="Your own name as it appears in narrations, to catch self-transfers. Repeatable.")
    args = parser.parse_args()

    transactions = parse_statement(args.csv_path, frozenset(args.self_name))
    conn = connect_writable(args.db)
    added = load(conn, transactions)
    print(f"Parsed {len(transactions)} rows, added {added} new transactions to {args.db}")


if __name__ == "__main__":
    main()
