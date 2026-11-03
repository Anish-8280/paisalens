"""Read statements as banks actually export them: PDF or CSV, with varying columns and date formats.

Unlike parse_statement, which takes one strict CSV format, this tolerates page furniture such as
opening balances and footers. It reports what it skipped, and checks the running balance when the
statement has one, because that catches misread amounts.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

from app.ingest.statements import StatementError, Transaction, build_transaction, finish_transactions

MAX_PDF_PAGES = 200
HEADER_FIELDS = (  # checked in order, so "withdrawal amount" is a debit before it can be an amount
    ("date", ("txn date", "tran date", "transaction date", "posting date", "date")),
    ("narration", ("narration", "description", "particulars", "transaction details", "details", "remarks")),
    ("type", ("dr/cr", "cr/dr", "dr / cr", "txn type")),
    ("debit", ("debit", "withdrawal", "dr amount", "paid out")),
    ("credit", ("credit", "deposit", "cr amount", "paid in")),
    ("balance", ("balance",)),
    ("amount", ("amount",)),
)
DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d %b %y",
                "%d-%b-%y", "%Y-%m-%d", "%d.%m.%Y", "%b %d, %Y")
NON_AMOUNTS = {"", "-", "--", "na", "n/a", "nil"}
AMOUNT_WORD = re.compile(r"^[₹(-]?\s*\d[\d,]*\.\d{2}\)?$")
FOOTER = re.compile(r"page \d|statement summary|opening balance|closing balance|grand total|^total\b|end of statement|"
                    r"generated|computer.generated|this is a system", re.I)
COLUMN_GAP_POINTS = 8
LINE_TOLERANCE_POINTS = 3


@dataclass
class ParsedStatement:
    transactions: list[Transaction] = field(default_factory=list)
    skipped_rows: int = 0
    balance_checked: int = 0
    balance_mismatches: int = 0


def map_columns(cells: list[str]) -> dict[str, int] | None:
    """Work out which column is which from a header row, or return None if this isn't one."""
    columns: dict[str, int] = {}
    for index, cell in enumerate(cells):
        text = " ".join((cell or "").lower().split())
        if not text or len(text) > 40:
            continue
        if "value" in text and "date" in text:  # the transaction date is the one that counts
            continue
        for name, aliases in HEADER_FIELDS:
            if name not in columns and any(alias in text for alias in aliases):
                columns[name] = index
                break
    has_amounts = ("debit" in columns and "credit" in columns) or "amount" in columns
    return columns if "date" in columns and "narration" in columns and has_amounts else None


def parse_date(text: str) -> date | None:
    text = " ".join((text or "").split())
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def parse_amount(text: str) -> tuple[int, str | None]:
    """Return (positive paise, 'debit'/'credit' if the text says). Unreadable text raises ValueError."""
    cleaned = (text or "").strip().lower()
    if cleaned in NON_AMOUNTS:
        return 0, None
    hint = None
    if cleaned.endswith(("cr", "cr.")):
        hint = "credit"
    elif cleaned.endswith(("dr", "dr.")):
        hint = "debit"
    if cleaned.startswith("-") or (cleaned.startswith("(") and cleaned.endswith(")")):
        hint = hint or "debit"
    digits = re.sub(r"[^\d.]", "", re.sub(r"rs\.?|inr|dr\.?|cr\.?", "", cleaned))
    try:
        paise = Decimal(digits) * 100
    except InvalidOperation as exc:
        raise ValueError(text) from exc
    if paise != paise.to_integral_value():
        raise ValueError(text)
    return int(paise), hint


def rows_to_statement(rows: list[list[str]], account: str, self_names: frozenset[str]) -> ParsedStatement:
    parsed = ParsedStatement()
    columns = None
    balances: list[tuple[int, int, int]] = []

    def cell(row, name):
        index = columns.get(name)
        return (row[index] or "") if index is not None and index < len(row) else ""

    last = None
    for row in rows:
        if map_columns(row):
            columns = map_columns(row)  # headers repeat on each page
            last = None
            continue
        if columns is None:
            continue
        joined = " ".join(c or "" for c in row).strip()
        if not joined:
            continue
        txn_date = parse_date(cell(row, "date"))
        if txn_date is None:
            # A wrapped narration continues the previous row. Anything else is page furniture.
            continuation = " ".join(cell(row, "narration").split())
            if last is not None and continuation and not FOOTER.search(joined) and not cell(row, "date").strip():
                last.narration += continuation if last.narration.endswith(("/", "-")) else " " + continuation
            else:
                parsed.skipped_rows += 1
                last = None
            continue

        narration = " ".join(cell(row, "narration").split())
        try:
            debit = parse_amount(cell(row, "debit"))[0]
            credit = parse_amount(cell(row, "credit"))[0]
            if "amount" in columns and not (debit or credit):
                paise, hint = parse_amount(cell(row, "amount"))
                kind = cell(row, "type").strip().lower()
                hint = "credit" if kind.startswith("c") else "debit" if kind.startswith("d") else hint
                if paise and hint is None:
                    raise ValueError("no direction")
                debit, credit = (paise, 0) if hint == "debit" else (0, paise)
            balance = parse_amount(cell(row, "balance"))[0] if "balance" in columns else None
        except ValueError:
            parsed.skipped_rows += 1
            last = None
            continue
        if bool(debit) == bool(credit) or not narration:
            parsed.skipped_rows += 1  # opening balance lines land here
            last = None
            continue

        last = build_transaction(account, txn_date, narration, debit, credit, self_names)
        parsed.transactions.append(last)
        if balance is not None:
            balances.append((debit, credit, balance))

    # Narrations can grow while continuations are read, so classify again on the complete text.
    for txn in parsed.transactions:
        rebuilt = build_transaction(txn.account, txn.txn_date, txn.narration,
                                    txn.amount_paise if txn.direction == "debit" else 0,
                                    txn.amount_paise if txn.direction == "credit" else 0, self_names)
        txn.merchant, txn.txn_type, txn.category = rebuilt.merchant, rebuilt.txn_type, rebuilt.category
    finish_transactions(parsed.transactions)
    parsed.balance_checked, parsed.balance_mismatches = _check_balances(balances)
    if columns is None:
        raise StatementError("Couldn't find the transaction table. It needs date, description and debit/credit (or amount) columns.")
    if not parsed.transactions:
        raise StatementError("Found the table headings but couldn't read any transactions under them.")
    return parsed


def _check_balances(balances: list[tuple[int, int, int]]) -> tuple[int, int]:
    """Compare each row's balance with the one before it, trying both orders because banks use either."""
    if len(balances) < 2:
        return 0, 0
    oldest_first = newest_first = 0
    for previous, current in zip(balances, balances[1:]):
        if current[2] != previous[2] - current[0] + current[1]:
            oldest_first += 1
        if previous[2] != current[2] - previous[0] + previous[1]:
            newest_first += 1
    return len(balances) - 1, min(oldest_first, newest_first)


def read_csv_rows(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _words_to_rows(page, header: dict | None) -> tuple[list[list[str]], dict | None]:
    """Rebuild rows from word positions, for statements whose tables have no ruled lines."""
    lines: dict[int, list[dict]] = {}
    for word in page.extract_words(x_tolerance=2):
        lines.setdefault(round(word["top"] / LINE_TOLERANCE_POINTS), []).append(word)

    rows = []
    for key in sorted(lines):
        words = sorted(lines[key], key=lambda w: w["x0"])
        cells: list[dict] = []
        for word in words:
            if cells and word["x0"] - cells[-1]["x1"] <= COLUMN_GAP_POINTS:
                cells[-1]["text"] += " " + word["text"]
                cells[-1]["x1"] = word["x1"]
            else:
                cells.append({"text": word["text"], "x0": word["x0"], "x1": word["x1"]})
        texts = [c["text"] for c in cells]
        if map_columns(texts):
            header = {"cells": cells, "columns": map_columns(texts)}
            rows.append(texts)
            continue
        if header is None:
            continue

        row = [""] * len(header["cells"])
        amount_indexes = [header["columns"][n] for n in ("debit", "credit", "balance", "amount") if n in header["columns"]]
        text_indexes = [i for i in range(len(row)) if i not in amount_indexes]
        for word in words:
            if amount_indexes and AMOUNT_WORD.match(word["text"]):
                target = min(amount_indexes, key=lambda i: abs(header["cells"][i]["x1"] - word["x1"]))
            else:
                target = next((i for i in reversed(text_indexes) if header["cells"][i]["x0"] <= word["x0"] + 2), text_indexes[0])
            row[target] = f"{row[target]} {word['text']}".strip()
        rows.append(row)
    return rows, header


def read_pdf_rows(data: bytes, password: str = "") -> tuple[list[list[str]], list[list[str]]]:
    """Return rows read two ways: from ruled tables, and from word positions."""
    try:
        with pdfplumber.open(io.BytesIO(data), password=password) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise StatementError(f"That PDF has more than {MAX_PDF_PAGES} pages.")
            table_rows: list[list[str]] = []
            word_rows: list[list[str]] = []
            header = None
            has_text = False
            for page in pdf.pages:
                has_text = has_text or bool((page.extract_text() or "").strip())
                for table in page.extract_tables():
                    table_rows.extend([cell or "" for cell in row] for row in table)
                rows, header = _words_to_rows(page, header)
                word_rows.extend(rows)
    except StatementError:
        raise
    except Exception as exc:
        chain = f"{exc!r} {exc.__cause__!r}".lower()
        if "password" in chain:
            raise StatementError("This PDF is password-protected, or the password is wrong.") from exc
        raise StatementError("That file couldn't be read as a PDF.") from exc
    if not has_text:
        raise StatementError("This PDF has no readable text, so it's probably a scan. Scanned statements aren't supported.")
    return table_rows, word_rows


def _best(candidates: list[list[list[str]]], account: str, self_names: frozenset[str]) -> ParsedStatement:
    """Use whichever reading holds together: fewest balance errors, then most transactions."""
    results, error = [], None
    for rows in candidates:
        try:
            results.append(rows_to_statement(rows, account, self_names))
        except StatementError as exc:
            error = error or exc
    if not results:
        raise error
    return min(results, key=lambda r: (r.balance_mismatches, -len(r.transactions), r.skipped_rows))


def parse_upload(filename: str, data: bytes, account: str, password: str = "",
                 self_names: frozenset[str] = frozenset()) -> ParsedStatement:
    if data[:5] == b"%PDF-":
        return _best(list(read_pdf_rows(data, password)), account, self_names)
    if filename.lower().endswith(".csv"):
        return rows_to_statement(read_csv_rows(data), account, self_names)
    raise StatementError("Upload a PDF or CSV statement. For Excel files, save as CSV first.")
