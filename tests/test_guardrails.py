import sqlite3

import pytest

from app.sql.guardrails import GuardrailError, run_query, validate


@pytest.mark.parametrize("sql", [
    "SELECT SUM(net_amount_paise) FROM spending",
    "SELECT merchant, COUNT(*) FROM spending GROUP BY merchant ORDER BY 2 DESC LIMIT 5",
    "WITH m AS (SELECT merchant FROM spending) SELECT * FROM m",
    "SELECT merchant FROM spending UNION SELECT merchant FROM investments",
    "SELECT s.merchant FROM spending s JOIN all_transactions a ON s.id = a.id LIMIT 1",
    "SELECT 150000 - (SELECT SUM(amount_paise) FROM investments)",
])
def test_legitimate_queries_run(db_path, sql):
    assert run_query(db_path, sql).rows


@pytest.mark.parametrize("sql", [
    "DELETE FROM transactions",
    "DROP TABLE transactions",
    "UPDATE transactions SET amount_paise = 1",
    "INSERT INTO transactions (merchant) VALUES ('x')",
    "CREATE TABLE x (a)",
    "PRAGMA table_info(transactions)",
    "ATTACH DATABASE 'x.db' AS x",
    "SELECT 1; DROP TABLE transactions",
    "SELECT 1; SELECT 2",
    "SELECT narration FROM transactions",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM main.spending",
    "SELECT * FROM pragma_table_info('transactions')",
    "SELECT load_extension('evil')",
    "SELECT readfile('.env')",
    "WITH x AS (SELECT narration FROM transactions) SELECT * FROM x",
    "SELECT * FROM (SELECT narration FROM transactions)",
    "WITH transactions AS (SELECT 1) SELECT 1",
    "SELECT * FROM (WITH transactions AS (SELECT 1) SELECT 1), transactions",
    "not sql at all",
    "",
])
def test_rejected_queries(db_path, sql):
    with pytest.raises(GuardrailError):
        run_query(db_path, sql)


def row_count(db_path):
    return sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def test_rejected_write_leaves_the_data_unchanged(db_path):
    before = row_count(db_path)
    with pytest.raises(GuardrailError):
        run_query(db_path, "DELETE FROM transactions")
    assert row_count(db_path) == before


def test_engine_blocks_hidden_columns_if_validation_is_bypassed(db_path, monkeypatch):
    """The authorizer has to hold on its own, in case a query ever gets past the parser check."""
    monkeypatch.setattr("app.sql.guardrails.validate", lambda sql: None)
    for sql in ("SELECT narration FROM transactions", "SELECT * FROM sqlite_master",
                "DELETE FROM transactions", "ATTACH DATABASE 'x.db' AS x", "PRAGMA table_info(transactions)"):
        with pytest.raises(GuardrailError):
            run_query(db_path, sql)


def test_connection_is_read_only_even_without_authorizer(db_path):
    from app.db import connect_readonly
    with pytest.raises(sqlite3.OperationalError):
        connect_readonly(db_path).execute("DELETE FROM transactions")


def test_runaway_query_times_out(db_path):
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT MAX(x) FROM c"
    with pytest.raises(GuardrailError, match="time limit"):
        run_query(db_path, sql, timeout_seconds=0.2)


def test_row_limit_flags_truncation_instead_of_hiding_it(db_path):
    result = run_query(db_path, "SELECT id FROM spending", row_limit=10)
    assert len(result.rows) == 10 and result.truncated
    assert not run_query(db_path, "SELECT COUNT(*) FROM spending", row_limit=10).truncated


def test_validate_gives_the_model_a_useful_message():
    with pytest.raises(GuardrailError, match="spending"):
        validate("SELECT * FROM transactions")
