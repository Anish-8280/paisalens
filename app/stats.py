"""Figures for the dashboard. Plain SQL over the views: no model is involved, so nothing leaves the machine."""
import sqlite3
from pathlib import Path
from statistics import median

from app.db import connect_readonly

TOP_MERCHANTS = 8
LARGEST_EXPENSES = 5
TOP_CATEGORIES = 8
RECURRING_MIN_MONTHS = 3
RECURRING_MAX_CHARGES_PER_MONTH = 1.5
RECURRING_AMOUNT_TOLERANCE = 0.3
ACTIVE_WITHIN_DAYS = 45
RECURRING_EXCLUDED_CATEGORIES = ("People", "Cash", "Fees")


def _month_filter(month: str | None) -> tuple[str, list]:
    return ("WHERE substr(txn_date, 1, 7) = ?", [month]) if month else ("", [])


def _rows(conn: sqlite3.Connection, sql: str, params: list | tuple = ()) -> list[dict]:
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _total(conn, sql, params) -> int:
    return conn.execute(sql, params).fetchone()[0] or 0


def recurring_payments(conn: sqlite3.Connection) -> list[dict]:
    """Merchants charged about once a month for a similar amount, such as subscriptions, rent and bills."""
    placeholders = ", ".join("?" for _ in RECURRING_EXCLUDED_CATEGORIES)
    charges = conn.execute(
        f"""SELECT merchant, txn_date, net_amount_paise FROM spending
            WHERE txn_type = 'purchase' AND category NOT IN ({placeholders}) ORDER BY txn_date""",
        RECURRING_EXCLUDED_CATEGORIES).fetchall()
    latest = conn.execute("SELECT MAX(txn_date) FROM all_transactions").fetchone()[0]

    by_merchant: dict[str, list[tuple[str, int]]] = {}
    for merchant, txn_date, amount in charges:
        by_merchant.setdefault(merchant, []).append((txn_date, amount))

    found = []
    for merchant, items in by_merchant.items():
        months = {txn_date[:7] for txn_date, _ in items}
        if len(months) < RECURRING_MIN_MONTHS or len(items) / len(months) > RECURRING_MAX_CHARGES_PER_MONTH:
            continue
        amounts = [amount for _, amount in items]
        typical = median(amounts)
        if any(abs(amount - typical) > typical * RECURRING_AMOUNT_TOLERANCE for amount in amounts):
            continue
        last_date = items[-1][0]
        days_ago = conn.execute("SELECT julianday(?) - julianday(?)", (latest, last_date)).fetchone()[0]
        found.append({"merchant": merchant, "typical_paise": round(typical), "months": len(months),
                      "last_date": last_date, "active": days_ago <= ACTIVE_WITHIN_DAYS})
    return sorted(found, key=lambda item: (not item["active"], -item["typical_paise"]))


def build_stats(db_path: str | Path, month: str | None = None) -> dict:
    conn = connect_readonly(db_path)
    try:
        where, params = _month_filter(month)
        months = [row[0] for row in conn.execute(
            "SELECT DISTINCT substr(txn_date, 1, 7) FROM all_transactions ORDER BY 1")]
        spent = _total(conn, f"SELECT SUM(net_amount_paise) FROM spending {where}", params)
        received = _total(conn, f"SELECT SUM(amount_paise) FROM income_received {where}", params)
        invested = _total(conn, f"SELECT SUM(amount_paise) FROM investments {where}", params)
        return {
            "months": months,
            "month": month,
            "summary": {
                "spent_paise": spent,
                "received_paise": received,
                "invested_paise": invested,
                "left_over_paise": received - spent - invested,
                "transactions": _total(conn, f"SELECT COUNT(*) FROM all_transactions {where}", params),
                "first_date": conn.execute("SELECT MIN(txn_date) FROM all_transactions").fetchone()[0],
                "last_date": conn.execute("SELECT MAX(txn_date) FROM all_transactions").fetchone()[0],
            },
            "monthly": _rows(conn, """SELECT substr(txn_date, 1, 7) AS month, SUM(net_amount_paise) AS spent_paise
                                      FROM spending GROUP BY month ORDER BY month"""),
            "categories": _rows(conn, f"""SELECT category, SUM(net_amount_paise) AS spent_paise FROM spending {where}
                                          GROUP BY category HAVING spent_paise > 0 ORDER BY spent_paise DESC""",
                                params)[:TOP_CATEGORIES],
            "merchants": _rows(conn, f"""SELECT merchant, category, SUM(net_amount_paise) AS spent_paise, COUNT(*) AS payments
                                         FROM spending {where} GROUP BY merchant HAVING spent_paise > 0
                                         ORDER BY spent_paise DESC LIMIT ?""", params + [TOP_MERCHANTS]),
            "largest": _rows(conn, f"""SELECT txn_date, merchant, category, net_amount_paise AS amount_paise FROM spending {where}
                                       ORDER BY net_amount_paise DESC LIMIT ?""", params + [LARGEST_EXPENSES]),
            "recurring": recurring_payments(conn),
        }
    finally:
        conn.close()


def list_transactions(db_path: str | Path, search: str = "", category: str = "", month: str = "",
                      limit: int = 50, offset: int = 0) -> dict:
    conditions, params = [], []
    if search:
        conditions.append("merchant LIKE ? ESCAPE '\\'")
        params.append("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if category:
        conditions.append("category = ?")
        params.append(category)
    if month:
        conditions.append("substr(txn_date, 1, 7) = ?")
        params.append(month)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    conn = connect_readonly(db_path)
    try:
        return {
            "total": conn.execute(f"SELECT COUNT(*) FROM all_transactions {where}", params).fetchone()[0],
            "rows": _rows(conn, f"""SELECT txn_date, account, merchant, category, txn_type, direction, amount_paise
                                    FROM all_transactions {where} ORDER BY txn_date DESC, id DESC LIMIT ? OFFSET ?""",
                          params + [limit, offset]),
            "categories": [row[0] for row in conn.execute("SELECT DISTINCT category FROM all_transactions ORDER BY 1")],
        }
    finally:
        conn.close()
