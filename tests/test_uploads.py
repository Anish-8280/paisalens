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
from tests.test_agent import FakeClient, text, tool

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


def service(db_path, tmp_path, monkeypatch, uploads=True, responses=()):
    monkeypatch.setenv("ENABLE_UPLOADS", "true" if uploads else "false")
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path / "analyses"))
    settings = Settings(database_path=str(db_path), as_of="2026-11-02")
    fake = FakeClient(*responses)
    app = create_app(settings, fake, UsageLimiter(100, 100))
    return TestClient(app, base_url="http://localhost", raise_server_exceptions=False), fake


WRITE = {"X-Requested-With": "paisalens"}


def new_analysis(client, data=None, name="s.csv", headers=WRITE, **form):
    return client.post("/analyses", files={"file": (name, data or as_csv(), "application/octet-stream")},
                       data={"account": "HDFC", **form}, headers=headers)


def analysis_id(client, **form):
    return new_analysis(client, **form).json()["analysis"]["id"]


def test_each_upload_starts_its_own_analysis_and_they_never_mix(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert client.get("/analyses").json()["analyses"] == []
    first = new_analysis(client, name="august_statement.csv").json()
    assert first["report"]["added"] == 5 and first["report"]["balance_mismatches"] == 0
    assert first["analysis"]["name"] == "august statement" and first["analysis"]["ai_allowed"] is False
    other_rows = b"Date,Narration,Debit,Credit\n01/10/2026,CARD/AMAZON,100.00,\n"
    second = new_analysis(client, other_rows, name="other.csv").json()["analysis"]["id"]
    one = client.get("/stats", params={"analysis": first["analysis"]["id"]}).json()
    two = client.get("/stats", params={"analysis": second}).json()
    assert one["summary"]["transactions"] == 5 and two["summary"]["transactions"] == 1
    assert len(client.get("/analyses").json()["analyses"]) == 2
    assert client.get("/stats").json()["summary"]["transactions"] > 1000  # the example is untouched


def test_adding_a_statement_to_an_analysis_skips_duplicates(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    identifier = analysis_id(client)

    def add(data):
        return client.post(f"/analyses/{identifier}/statements", files={"file": ("s.csv", data, "text/csv")}, data={"account": "HDFC"}, headers=WRITE)

    assert add(as_csv()).json()["report"]["duplicates"] == 5
    more = ROWS + [["20/09/26", "UPI-UBER-uber@axis-UTIB0000001-5-RIDE", "U3", "20/09/26", "100.00", "", "99,149.50"]]
    assert add(as_csv(more)).json()["report"]["added"] == 1
    assert client.get("/stats", params={"analysis": identifier}).json()["summary"]["transactions"] == 6


def test_a_bad_file_leaves_no_empty_analysis_behind(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert new_analysis(client, b"nonsense", name="s.csv").status_code == 422
    assert client.get("/analyses").json()["analyses"] == []


def test_the_uploaded_file_is_not_kept(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    identifier = new_analysis(client, ruled_pdf(), "statement.pdf").json()["analysis"]["id"]
    assert [path.name for path in (tmp_path / "analyses").iterdir()] == [f"{identifier}.db"]


def test_uploads_are_off_by_default_on_a_public_deployment(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch, uploads=False)
    assert new_analysis(client).status_code == 404
    fake_id = "a" * 32
    assert client.get("/stats", params={"analysis": fake_id}).status_code == 404
    assert client.delete(f"/analyses/{fake_id}", headers=WRITE).status_code == 404
    assert client.get("/config").json()["uploads_enabled"] is False
    assert client.get("/analyses").json()["analyses"] == []
    assert client.get("/stats").status_code == 200  # the example still works
    assert not (tmp_path / "analyses").exists()


def test_writes_need_the_custom_header_so_other_sites_cannot_trigger_them(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    identifier = analysis_id(client)
    assert new_analysis(client, headers={}).status_code == 403
    assert client.delete(f"/analyses/{identifier}").status_code == 403
    assert client.patch(f"/analyses/{identifier}", json={"ai_allowed": True}).status_code == 403
    body = {"analysis": identifier, "merchant": "x", "category": "Other"}
    assert client.patch("/categories", json=body).status_code == 403


def test_foreign_hosts_are_refused_when_uploads_are_on(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert client.get("/config", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/config").status_code == 200


@pytest.mark.parametrize("identifier", ["../secrets", "..%2Fsecrets", "A" * 32, "a" * 31, "a" * 32])
def test_bad_or_unknown_analysis_ids_are_refused(db_path, tmp_path, monkeypatch, identifier):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert client.get("/stats", params={"analysis": identifier}).status_code == 404
    assert client.post("/ask", json={"question": "x", "analysis": identifier}).status_code == 404


def test_questions_stay_off_until_turned_on_for_that_analysis(db_path, tmp_path, monkeypatch):
    sql = "SELECT COUNT(*) AS n FROM all_transactions"
    client, fake = service(db_path, tmp_path, monkeypatch, responses=(tool(sql), text("Five.")))
    identifier, other = analysis_id(client), analysis_id(client)
    response = client.post("/ask", json={"question": "How many?", "analysis": identifier})
    assert response.status_code == 403 and fake.requests == []
    assert client.patch(f"/analyses/{identifier}", json={"ai_allowed": True}, headers=WRITE).json()["ai_allowed"] is True
    assert client.post("/ask", json={"question": "How many?", "analysis": other}).status_code == 403
    assert fake.requests == []
    assert client.post("/ask", json={"question": "How many?", "analysis": identifier}).status_code == 200
    assert '"n": 5' in fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert "2026-11-02" not in fake.requests[0]["messages"][0]["content"]  # real data uses the real date


def test_answers_carry_a_table_and_chart_built_from_the_database_result(db_path, tmp_path, monkeypatch):
    sql = "SELECT merchant, SUM(net_amount_paise) AS total_paise FROM spending GROUP BY merchant ORDER BY 2 DESC LIMIT 3"
    client, _ = service(db_path, tmp_path, monkeypatch, responses=(tool(sql), text("Here.")))
    table = client.post("/ask", json={"question": "Top?"}).json()["table"]
    assert table["columns"] == ["Merchant", "Total"] and len(table["rows"]) == 3
    assert table["rows"][0][1].startswith("₹")
    assert table["chart"]["labels"] == [row[0] for row in table["rows"]]
    assert table["chart"]["values_paise"] == sorted(table["chart"]["values_paise"], reverse=True)


def test_single_value_answers_have_no_table(db_path, tmp_path, monkeypatch):
    sql = "SELECT SUM(net_amount_paise) AS total_paise FROM spending"
    client, _ = service(db_path, tmp_path, monkeypatch, responses=(tool(sql), text("Lots.")))
    assert client.post("/ask", json={"question": "Total?"}).json()["table"] is None


def test_follow_up_questions_send_the_earlier_conversation(db_path, tmp_path, monkeypatch):
    client, fake = service(db_path, tmp_path, monkeypatch, responses=(text("In October, ₹5."),))
    history = [{"question": "Swiggy in September?", "answer": "**₹10** on Swiggy."}]
    assert client.post("/ask", json={"question": "And October?", "history": history}).status_code == 200
    messages = fake.requests[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == "**₹10** on Swiggy." and "And October?" in messages[2]["content"]


def test_history_length_and_size_are_bounded(db_path, tmp_path, monkeypatch):
    client, fake = service(db_path, tmp_path, monkeypatch)
    turn = {"question": "q", "answer": "a"}
    assert client.post("/ask", json={"question": "x", "history": [turn] * 7}).status_code == 422
    assert client.post("/ask", json={"question": "x", "history": [{"question": "q", "answer": "a" * 2001}]}).status_code == 422
    assert fake.requests == []


def test_category_correction_applies_now_and_to_later_statements(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    identifier = analysis_id(client)
    body = {"analysis": identifier, "merchant": "Rahul Verma", "category": "Rent"}
    assert client.patch("/categories", json=body, headers=WRITE).json()["changed"] == 1
    later = ROWS + [["20/09/26", "UPI-RAHUL VERMA-rahul@oksbi-SBIN0000001-125-RENT", "U3", "20/09/26", "100.00", "", "99,149.50"]]
    client.post(f"/analyses/{identifier}/statements", files={"file": ("s.csv", as_csv(later), "text/csv")}, data={"account": "HDFC"}, headers=WRITE)
    rows = client.get("/transactions", params={"analysis": identifier, "search": "Rahul"}).json()["rows"]
    assert len(rows) == 2 and {row["category"] for row in rows} == {"Rent"}


def test_the_example_cannot_be_changed_or_deleted(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    body = {"analysis": "example", "merchant": "Swiggy", "category": "Other"}
    assert client.patch("/categories", json=body, headers=WRITE).status_code == 403
    assert client.delete("/analyses/example", headers=WRITE).status_code == 403
    assert client.patch("/analyses/example", json={"name": "x"}, headers=WRITE).status_code == 403
    files = {"file": ("s.csv", as_csv(), "text/csv")}
    assert client.post("/analyses/example/statements", files=files, headers=WRITE).status_code == 403


def test_unknown_category_is_refused(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    body = {"analysis": analysis_id(client), "merchant": "x", "category": "Nope"}
    assert client.patch("/categories", json=body, headers=WRITE).status_code == 422


def test_model_cannot_reach_settings_or_raw_text(db_path, tmp_path, monkeypatch):
    from app.sql.guardrails import GuardrailError, run_query
    client, _ = service(db_path, tmp_path, monkeypatch)
    path = tmp_path / "analyses" / f"{analysis_id(client)}.db"
    for sql in ("SELECT * FROM category_overrides", "SELECT * FROM analysis_meta", "SELECT narration FROM transactions"):
        with pytest.raises(GuardrailError):
            run_query(path, sql)


def test_rename_and_delete_touch_only_that_analysis(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    keep, remove = analysis_id(client), analysis_id(client)
    assert client.patch(f"/analyses/{keep}", json={"name": "  Holiday   account "}, headers=WRITE).json()["name"] == "Holiday account"
    assert client.delete(f"/analyses/{remove}", headers=WRITE).json()["deleted"] is True
    assert client.get("/stats", params={"analysis": remove}).status_code == 404
    assert client.get("/stats", params={"analysis": keep}).status_code == 200
    assert [a["id"] for a in client.get("/analyses").json()["analyses"]] == [keep]


def test_data_uploaded_before_analyses_existed_is_kept(db_path, tmp_path, monkeypatch):
    conn = connect_writable(tmp_path / "my.db")
    load(conn, parse_upload("s.csv", as_csv(), "HDFC").transactions)
    conn.close()
    client, _ = service(db_path, tmp_path, monkeypatch)
    analyses = client.get("/analyses").json()["analyses"]
    assert [a["name"] for a in analyses] == ["My analysis"] and analyses[0]["transactions"] == 5
    assert analyses[0]["ai_allowed"] is False and not (tmp_path / "my.db").exists()


def test_oversized_upload_is_refused(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert new_analysis(client, b"x" * (10 * 1024 * 1024 + 1)).status_code == 413


def test_bad_month_is_refused(db_path, tmp_path, monkeypatch):
    client, _ = service(db_path, tmp_path, monkeypatch)
    assert client.get("/stats", params={"month": "2026-9' OR 1=1"}).status_code == 422
