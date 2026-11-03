"""The agent's only tool. It can read through the guardrails and has no way to write or reach the network."""
import json
from pathlib import Path

from app.sql.guardrails import GuardrailError, QueryResult, run_query

MAX_TABLE_ROWS = 50
MAX_CHART_POINTS = 24

QUERY_TOOL = {
    "name": "query_transactions",
    "description": (
        "Run one read-only SQLite SELECT over the user's transactions and return the rows as JSON. "
        "Use it for every number in your answer. Alias money columns with a `_paise` suffix, and the "
        "result will include a formatted `_rupees` value to quote. Compute totals in SQL, because a "
        "result marked truncated is incomplete."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
        "required": ["sql"],
        "additionalProperties": False,
    },
}


def format_inr(paise: int) -> str:
    """12345678 -> '₹1,23,456.78', using Indian digit grouping."""
    sign = "-" if paise < 0 else ""
    rupees, fraction = divmod(abs(int(paise)), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return f"{sign}₹{digits}.{fraction:02d}"


def serialise(result: QueryResult) -> str:
    rows = []
    for row in result.rows:
        record = dict(zip(result.columns, row))
        for column, value in zip(result.columns, row):
            if column.endswith("_paise") and isinstance(value, (int, float)):
                record[column.removesuffix("_paise") + "_rupees"] = format_inr(round(value))
        rows.append(record)
    return json.dumps({"row_count": len(rows), "truncated": result.truncated, "rows": rows}, ensure_ascii=False)


def _column_label(column: str) -> str:
    return column.removesuffix("_paise").replace("_", " ").strip().capitalize() or column


def present_result(result: QueryResult | None) -> dict | None:
    """Turn a multi-row query result into a table, and a chart when it's labels against one money column.

    Built directly from the database result, so what's shown can't differ from what was computed.
    """
    if result is None or len(result.rows) < 2:
        return None
    keep = [index for index, column in enumerate(result.columns) if column.lower() != "id"]
    money = [index for index in keep if result.columns[index].endswith("_paise")]

    def display(index, value):
        if value is None:
            return ""
        return format_inr(round(value)) if index in money and isinstance(value, (int, float)) else value

    presented = {
        "columns": [_column_label(result.columns[index]) for index in keep],
        "numeric": [isinstance(result.rows[0][index], (int, float)) for index in keep],
        "rows": [[display(index, row[index]) for index in keep] for row in result.rows[:MAX_TABLE_ROWS]],
        "truncated": result.truncated or len(result.rows) > MAX_TABLE_ROWS,
        "chart": None,
    }
    labels = [index for index in keep if isinstance(result.rows[0][index], str)]
    if len(keep) == 2 and len(money) == 1 and len(labels) == 1 and len(result.rows) <= MAX_CHART_POINTS:
        presented["chart"] = {
            "labels": [str(row[labels[0]]) for row in result.rows],
            "values_paise": [round(row[money[0]] or 0) for row in result.rows],
            "series": _column_label(result.columns[money[0]]),
        }
    return presented


def run_tool(db_path: str | Path, sql: str, row_limit: int, timeout_seconds: float) -> tuple[str, QueryResult | None]:
    """Return (text for the model, result). A rejected query comes back as text so the model can correct it."""
    try:
        result = run_query(db_path, sql, row_limit, timeout_seconds)
    except GuardrailError as exc:
        return str(exc), None
    return serialise(result), result
