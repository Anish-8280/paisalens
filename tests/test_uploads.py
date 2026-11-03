"""Bank-style statements, uploads, stats and the safeguards around real data."""
import io
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from app.agent.loop import Settings
from app.db import connect_writable
from app.ingest.parsers import map_columns, parse_amount, parse_date, parse_upload
from app.ingest.statements import StatementError, classify, load, relink_internal_transfers
from app.limits import UsageLimiter
from app.main import create_app
from app.stats import build_stats, list_transactions, recurring_payments
from tests.test_agent import FakeClient, text

HEADER = ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]
ROWS = [
    ["01/09/26", "SALARY SEP ACME TECHNOLOGIES", "N1", "01/09/26", "", "1,00,000.00", "1,00,000.00"],
    ["03/09/26", "UPI-SWIGGY-swiggy@axis-UTIB0000001-123-FOOD", "U1", "03/09/26", "500.00", "", "99,500.00"],
    ["04/09/26", "UPI-RAHUL VERMA-rahul@oksbi-SBIN0000001-124-LUNCH", "U2", "04/09/26", "250.50", "", "99,249.50"],
    ["05/09/26", "POS 1234 AMAZON SELLER", "P1", "05/09/26", "2,000.00", "", "97,249.50"],
    ["09/09/26", "REFUND AMAZON SELLER", "R1", "09/09/26", "", "2,000.00", "99,249.50"],
]
EXPECTED_TYPES = ["income", "purchase", "p2p", "purchase", "refund"]


def ruled_pdf(rows=ROWS, password=None):
    """A statement with a gridded table, as many banks produce."""
    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4)
    if password:
        document.encrypt = password
    table = Table([HEADER] + rows)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 6)]))
    document.build([table])
    return buffer.getvalue()


def unruled_pdf(rows=ROWS):
    """The same data with no table lines, so columns can only be found from word positions."""
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    page.setFont("Helvetica", 7)
    y = 800
    page.drawString(40, y, "HDFC BANK Statement of account")
    y -= 30
    lefts = {0: 40, 1: 90}
    rights = {4: 400, 5: 470, 6: 550}
    page.drawString(40, y, "Date")
    page.drawString(90, y, "Narration")
    page.drawRightString(400, y, "Withdrawal Amt.")
    page.drawRightString(470, y, "Deposit Amt.")
    page.drawRightString(550, y, "Closing Balance")
    for row in rows:
        y -= 14
        for index, x in lefts.items():
            page.drawString(x, y, row[index])
        for index, x in rights.items():
            page.drawRightString(x, y, row[index])
    page.drawString(40, y - 30, "Page 1 of 1. This is a system generated statement.")
    page.save()
    return buffer.getvalue()


def as_csv(rows=ROWS, preamble="Statement for account XX1234\n\n"):
    lines = [",".join(f'"{cell}"' for cell in [*HEADER]) ] + [",".join(f'"{cell}"' for cell in row) for row in rows]
    return (preamble + "\n".join(lines) + "\n").encode()


@pytest.mark.parametrize("name, data", [("s.pdf", ruled_pdf()), ("s.pdf", unruled_pdf()), ("s.csv", as_csv())],
                         ids=["ruled_pdf", "unruled_pdf", "bank_csv"])
def test_every_format_gives_the_same_transactions(name, data):
    parsed = parse_upload(name, data, "HDFC")
    assert [t.txn_type for t in parsed.transactions] == EXPECTED_TYPES
    assert [t.amount_paise for t in parsed.transactions] == [10000000, 50000, 25050, 200000, 200000]
    assert [t.direction for t in parsed.transactions] == ["credit", "debit", "debit", "debit", "credit"]
    assert parsed.transactions[0].txn_date == date(2026, 9, 1)
    assert parsed.transactions[1].merchant == "Swiggy" and parsed.transactions[2].merchant == "Rahul Verma"
    assert parsed.balance_checked == 4 and parsed.balance_mismatches == 0


def test_a_misread_amount_is_caught_by_the_balance_check():
    wrong = [list(row) for row in ROWS]
    wrong[1][4] = "5,000.00"
    parsed = parse_upload("s.csv", as_csv(wrong), "HDFC")
    assert parsed.balance_mismatches >= 1


def test_newest_first_statements_pass_the_balance_check():
    parsed = parse_upload("s.csv", as_csv(list(reversed(ROWS))), "HDFC")
    assert parsed.balance_mismatches == 0


def test_single_amount_column_with_drcr():
    data = ("Txn Date,Description,Amount,Dr/Cr,Balance\n"
            "01-Sep-2026,NEFT SALARY ACME,1000.00,CR,1000.00\n"
            "02-Sep-2026,CARD AMAZON,100.00,DR,900.00\n").encode()
    parsed = parse_upload("s.csv", data, "A")
    assert [t.direction for t in parsed.transactions] == ["credit", "debit"]
    assert parsed.balance_mismatches == 0


def test_amount_with_no_direction_is_skipped_rather_than_guessed():
    data = b"Date,Description,Amount\n01/09/2026,MYSTERY,100.00\n01/09/2026,KNOWN,-50.00\n"
    parsed = parse_upload("s.csv", data, "A")
    assert parsed.skipped_rows == 1 and [t.narration for t in parsed.transactions] == ["KNOWN"]


def test_wrapped_narrations_and_opening_balance_lines():
    data = ('Date,Narration,Debit,Credit,Balance\n'
            '01/09/2026,OPENING BALANCE,,,1000.00\n'
            '02/09/2026,UPI/123/SWIGGY,100.00,,900.00\n'
            ',LTD/swiggy@axis,,,\n'
            'Page 1 of 2,,,,\n').encode()
    parsed = parse_upload("s.csv", data, "A")
    assert len(parsed.transactions) == 1 and parsed.transactions[0].narration == "UPI/123/SWIGGY LTD/swiggy@axis"
    assert parsed.skipped_rows == 2


def test_value_date_column_is_not_mistaken_for_the_transaction_date():
    columns = map_columns(["Value Date", "Txn Date", "Description", "Debit", "Credit"])
    assert columns["date"] == 1


@pytest.mark.parametrize("raw, expected", [("01/09/26", date(2026, 9, 1)), ("1 Sep 2026", date(2026, 9, 1)),
                                           ("2026-09-01", date(2026, 9, 1)), ("01-Sep-26", date(2026, 9, 1)), ("nope", None)])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw, expected", [("₹1,234.50", (123450, None)), ("500.00 Cr", (50000, "credit")),
                                           ("Rs. 20 Dr", (2000, "debit")), ("-", (0, None)), ("-50.00", (5000, "debit"))])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("name, data, message", [
    ("s.txt", b"hello", "PDF or CSV"),
    ("s.csv", b"a,b,c\n1,2,3\n", "transaction table"),
    ("s.pdf", b"%PDF-1.4 not really", "couldn't be read"),
])
def test_unusable_files_explain_themselves(name, data, message):
    with pytest.raises(StatementError, match=message):
        parse_upload(name, data, "A")


def test_scanned_pdf_is_explained():
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    page.rect(10, 10, 100, 100)
    page.save()
    with pytest.raises(StatementError, match="scan"):
        parse_upload("s.pdf", buffer.getvalue(), "A")


def encrypted_pdf(password):
    from reportlab.lib.pdfencrypt import StandardEncryption
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, encrypt=StandardEncryption(password, canPrint=1))
    page.drawString(40, 800, "Date Narration Debit Credit")
    page.save()
    return buffer.getvalue()


def test_password_protected_pdf_asks_for_the_password():
    with pytest.raises(StatementError, match="password"):
        parse_upload("s.pdf", encrypted_pdf("secret"), "A", password="wrong")


@pytest.mark.parametrize("narration, direction, expected", [
    ("UPI/DR/12345/NEHA GUPTA/HDFC/neha@oksbi/Lunch", "debit", ("Neha Gupta", "p2p", "People")),
    ("UPI-ZOMATO LTD-zomato@hdfc-HDFC0000001-99-ORDER", "debit", ("Zomato", "purchase", "Food Delivery")),
    ("BY TRANSFER FROM SOMEONE", "credit", ("By Transfer From Someone", "income", "Other Income")),
    ("NEFT CR-SBIN-ACME-SALARY OCT", "credit", ("Acme", "income", "Salary")),
    ("NEFT CR-SAMP0000123-NORTHWIND LABS PVT LTD-SALARY", "credit", ("Northwind Labs", "income", "Salary")),
])
def test_real_world_narrations(narration, direction, expected):
    assert classify(narration, direction) == expected


def test_transfers_are_paired_across_separately_uploaded_statements(tmp_path):
    conn = connect_writable(tmp_path / "t.db")
    first = parse_upload("a.csv", b"Date,Narration,Debit,Credit\n15/09/2026,IMPS/1/TRANSFER/ME,1000.00,\n", "A").transactions
    second = parse_upload("b.csv", b"Date,Narration,Debit,Credit\n15/09/2026,IMPS/1/TRANSFER/ME,,1000.00\n", "B").transactions
    load(conn, first)
    load(conn, second)
    assert relink_internal_transfers(conn) == 1
    assert {row[0] for row in conn.execute("SELECT txn_type FROM transactions")} == {"internal_transfer"}


def test_stats_match_direct_totals(db_path):
    stats = build_stats(db_path)
    conn = sqlite3.connect(db_path)
    assert stats["summary"]["spent_paise"] == conn.execute("SELECT SUM(net_amount_paise) FROM spending").fetchone()[0]
    assert sum(m["spent_paise"] for m in stats["monthly"]) == stats["summary"]["spent_paise"]
    assert len(stats["months"]) == 12 and len(stats["merchants"]) == 8
    assert stats["merchants"] == sorted(stats["merchants"], key=lambda m: -m["spent_paise"])


def test_month_filter_narrows_everything_but_the_trend(db_path):
    stats = build_stats(db_path, "2026-09")
    direct = sqlite3.connect(db_path).execute(
        "SELECT SUM(net_amount_paise) FROM spending WHERE txn_date LIKE '2026-09%'").fetchone()[0]
    assert stats["summary"]["spent_paise"] == direct and len(stats["monthly"]) == 12
    assert all(item["txn_date"].startswith("2026-09") for item in stats["largest"])


def test_recurring_finds_subscriptions_and_not_frequent_merchants(db_path):
    found = {item["merchant"]: item for item in recurring_payments(sqlite3.connect(db_path))}
    assert {"Netflix", "Spotify", "Hotstar", "Rent", "Airtel", "Jio"} <= set(found)
    assert not {"Swiggy", "Uber", "Own Account", "Credit Card Payment"} & set(found)
    assert found["Netflix"]["typical_paise"] == 64900 and found["Netflix"]["active"]


def test_transaction_search_treats_wildcards_literally_and_pages(db_path):
    assert list_transactions(db_path, search="%")["total"] == 0
    page = list_transactions(db_path, search="swig", limit=5, offset=5)
    assert len(page["rows"]) == 5 and page["total"] > 10
    assert all("swiggy" in row["merchant"].lower() for row in page["rows"])
    assert "narration" not in page["rows"][0]


def service(db_path, tmp_path, monkeypatch, uploads=True, real_data=False, responses=()):
    monkeypatch.setenv("ENABLE_UPLOADS", "true" if uploads else "false")
    monkeypatch.setenv("ALLOW_REAL_DATA_TO_MODEL", "true" if real_data else "false")
    monkeypatch.setenv("MY_DATABASE_PATH", str(tmp_path / "my.db"))
    settings = Settings(database_path=str(db_path), as_of="2026-11-02")
    fake = FakeClient(*responses)
    app = create_app(settings, fake, UsageLimiter(100, 100))
    return TestClient(app, base_url="http://localhost", raise_server_exceptions=False), fake


WRITE = {"X-Requested-With": "paisalens"}


def post_statement(client, data=None, name="s.csv", headers=WRITE, **form):
    return client.post("/upload", files={"file": (name, data or as_csv(), "application/octet-stream")},
                       data={"account": "HDFC", **form}, headers=headers)


def test_upload_then_stats_and_reimport(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert client.get("/stats", params={"dataset": "mine"}).status_code == 404
    first = post_statement(client).json()
    assert first["added"] == 5 and first["balance_mismatches"] == 0
    assert post_statement(client).json()["duplicates"] == 5
    stats = client.get("/stats", params={"dataset": "mine"}).json()
    assert stats["summary"]["transactions"] == 5
    assert client.get("/config").json()["has_my_data"] is True


def test_the_uploaded_file_is_not_kept(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    post_statement(client, ruled_pdf(), "statement.pdf")
    assert [path.name for path in tmp_path.iterdir()] == ["my.db"]


def test_uploads_are_off_by_default_on_a_public_deployment(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch, uploads=False)
    assert post_statement(client).status_code == 404
    assert client.get("/stats", params={"dataset": "mine"}).status_code == 404
    assert client.delete("/my-data", headers=WRITE).status_code == 404
    assert client.get("/config").json()["uploads_enabled"] is False
    assert client.get("/stats").status_code == 200  # the demo still works
    assert not (tmp_path / "my.db").exists()


def test_writes_need_the_custom_header_so_other_sites_cannot_trigger_them(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert post_statement(client, headers={}).status_code == 403
    assert client.delete("/my-data").status_code == 403
    assert client.patch("/categories", json={"merchant": "x", "category": "Other"}).status_code == 403


def test_foreign_hosts_are_refused_when_uploads_are_on(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert client.get("/config", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/config").status_code == 200


def test_questions_on_my_data_stay_off_until_explicitly_allowed(db_path, tmp_path, monkeypatch):
    client, fake = service(db_path, tmp_path, monkeypatch)
    post_statement(client)
    response = client.post("/ask", json={"question": "How much?", "dataset": "mine"})
    assert response.status_code == 403 and fake.requests == []


def test_questions_on_my_data_use_my_database_and_real_dates_when_allowed(db_path, tmp_path, monkeypatch):
    sql = "SELECT COUNT(*) AS n FROM all_transactions"
    from tests.test_agent import tool
    client, fake = service(db_path, tmp_path, monkeypatch, real_data=True, responses=(tool(sql), text("Five.")))
    post_statement(client)
    assert client.post("/ask", json={"question": "How many?", "dataset": "mine"}).status_code == 200
    assert '"n": 5' in fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert "2026-11-02" not in fake.requests[0]["messages"][0]["content"]


def test_demo_data_is_never_mixed_with_mine(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    before = client.get("/stats").json()["summary"]["transactions"]
    post_statement(client)
    assert client.get("/stats").json()["summary"]["transactions"] == before


def test_category_correction_applies_now_and_to_later_uploads(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    post_statement(client)
    change = client.patch("/categories", json={"merchant": "Rahul Verma", "category": "Rent"}, headers=WRITE)
    assert change.json()["changed"] == 1
    later = ROWS + [["20/09/26", "UPI-RAHUL VERMA-rahul@oksbi-SBIN0000001-125-RENT", "U3", "20/09/26", "100.00", "", "99,149.50"]]
    post_statement(client, as_csv(later))
    rows = client.get("/transactions", params={"dataset": "mine", "search": "Rahul"}).json()["rows"]
    assert len(rows) == 2 and {row["category"] for row in rows} == {"Rent"}


def test_unknown_category_is_refused(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    post_statement(client)
    assert client.patch("/categories", json={"merchant": "x", "category": "Nope"}, headers=WRITE).status_code == 422


def test_model_cannot_reach_override_table_or_raw_text(db_path, tmp_path, monkeypatch):
    from app.sql.guardrails import GuardrailError, run_query
    client, _ = service(db_path, tmp_path, monkeypatch)
    post_statement(client)
    with pytest.raises(GuardrailError):
        run_query(tmp_path / "my.db", "SELECT * FROM category_overrides")


def test_delete_removes_my_data_only(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    post_statement(client)
    assert client.delete("/my-data", headers=WRITE).json()["deleted"] is True
    assert not (tmp_path / "my.db").exists()
    assert client.get("/stats", params={"dataset": "mine"}).status_code == 404
    assert client.get("/stats").status_code == 200


def test_oversized_upload_is_refused(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert post_statement(client, b"x" * (10 * 1024 * 1024 + 1)).status_code == 413


def test_bad_month_is_refused(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert client.get("/stats", params={"month": "2026-9' OR 1=1"}).status_code == 422
