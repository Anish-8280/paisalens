"""Checks that stand between model-written SQL and the database.

Two independent layers: an AST check gives the model a useful error message, and a SQLite
authorizer on a read-only connection is what actually enforces the rules at execution time.
"""
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

from app.db import connect_readonly

ALLOWED_TABLES = frozenset({"spending", "investments", "income_received", "all_transactions"})
PROTECTED_TABLES = frozenset({"transactions"})
HIDDEN_COLUMNS = frozenset({"narration", "txn_hash"})
FORBIDDEN_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})
PROGRESS_CHECK_INTERVAL = 1000  # SQLite VM instructions between timeout checks


class GuardrailError(ValueError):
    """The query was rejected. The message is safe to return to the model so it can retry."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False
    sql: str = field(default="")


def validate(sql: str) -> None:
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except sqlglot.errors.SqlglotError as exc:
        raise GuardrailError(f"Could not parse SQL: {exc}") from exc

    if len(statements) != 1:
        raise GuardrailError("Send exactly one statement.")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise GuardrailError("Only SELECT queries are allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    # A nested CTE named after a real table would let an outer reference to it slip past the check below.
    if cte_names & (PROTECTED_TABLES | ALLOWED_TABLES) or any(name.startswith("sqlite_") for name in cte_names):
        raise GuardrailError("CTE names must not match existing tables.")
    for table in statement.find_all(exp.Table):
        name = table.name.lower()
        if table.db or table.catalog or (name not in ALLOWED_TABLES and name not in cte_names):
            raise GuardrailError(f"Table {table.sql()!r} is not available. Use: {', '.join(sorted(ALLOWED_TABLES))}.")

    for func in statement.find_all(exp.Anonymous):
        if func.name.lower() in FORBIDDEN_FUNCTIONS:
            raise GuardrailError(f"Function {func.name} is not allowed.")


def _authorizer(action, arg1, arg2, _db_name, _source):
    if action in (sqlite3.SQLITE_SELECT, getattr(sqlite3, "SQLITE_RECURSIVE", -1)):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        # Views resolve to reads on `transactions`, so the base table has to stay readable,
        # but its raw-text columns and the system tables never are, however the query got here.
        hidden = (arg1 or "").lower() in PROTECTED_TABLES and (arg2 or "").lower() in HIDDEN_COLUMNS
        return sqlite3.SQLITE_DENY if hidden or (arg1 or "").lower().startswith("sqlite_") else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if arg2.lower() in FORBIDDEN_FUNCTIONS else sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def run_query(db_path: str | Path, sql: str, row_limit: int = 500, timeout_seconds: float = 5.0) -> QueryResult:
    """Validate and run one query. Too many rows sets `truncated` rather than silently dropping them."""
    validate(sql)

    conn = connect_readonly(db_path)
    deadline = time.monotonic() + timeout_seconds
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_CHECK_INTERVAL)
    conn.set_authorizer(_authorizer)
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(row_limit + 1)
        columns = [column[0] for column in cursor.description or []]
    except sqlite3.DatabaseError as exc:
        if "interrupted" in str(exc):
            raise GuardrailError(f"Query exceeded the {timeout_seconds}s time limit.") from exc
        raise GuardrailError(f"Query failed: {exc}") from exc
    finally:
        conn.close()

    return QueryResult(columns, rows[:row_limit], truncated=len(rows) > row_limit, sql=sql)
