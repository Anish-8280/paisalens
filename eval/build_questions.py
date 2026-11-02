"""Generate eval/questions.jsonl: the benchmark questions and hand-written gold SQL.

Gold answers come from running gold_sql at eval time. Whether the views themselves are right
is checked separately in tests/test_ingest.py, against the generator's truth file.

Every question is pinned to AS_OF. The split is stable per question, and only `dev` may be
looked at while tuning prompts. `test` is what gets reported.
"""
import hashlib
import json
from pathlib import Path

AS_OF = "2026-11-02"
TEST_SHARE_PERCENT = 40


def month_range(year: int, month: int) -> tuple[str, str]:
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year}-{month:02d}-01", f"{end_year}-{end_month:02d}-01"


MONTHS = {"September 2026": (2026, 9), "January 2026": (2026, 1), "March 2026": (2026, 3), "June 2026": (2026, 6)}


def spend_sql(where: str) -> str:
    return f"SELECT COALESCE(SUM(net_amount_paise), 0) FROM spending WHERE {where}"


def build() -> list[dict]:
    questions = []

    def add(group, question, gold_sql, kind="money", ordered=False):
        questions.append({"group": group, "question": question, "gold_sql": gold_sql, "kind": kind, "ordered": ordered})

    for merchant in ("Swiggy", "Zomato", "Amazon", "Uber", "Flipkart"):
        for label, (year, month) in MONTHS.items():
            start, end = month_range(year, month)
            add("merchant_month", f"How much did I spend on {merchant} in {label}?",
                spend_sql(f"merchant = '{merchant}' AND txn_date >= '{start}' AND txn_date < '{end}'"))

    for category in ("Food Delivery", "Groceries", "Shopping", "Transport", "Bills"):
        for label, (year, month) in MONTHS.items():
            start, end = month_range(year, month)
            add("category_month", f"What did I spend on {category.lower()} in {label}?",
                spend_sql(f"category = '{category}' AND txn_date >= '{start}' AND txn_date < '{end}'"))

    # Relative dates, pinned by AS_OF: last month is October 2026.
    oct_range = "txn_date >= '2026-10-01' AND txn_date < '2026-11-01'"
    fy_range = "txn_date >= '2026-04-01' AND txn_date < '2027-04-01'"
    add("relative", "How much did I spend last month?", spend_sql(oct_range))
    add("relative", "How much did I spend on food delivery last month?", spend_sql(f"category = 'Food Delivery' AND {oct_range}"))
    add("relative", "What did I spend on Swiggy last month?", spend_sql(f"merchant = 'Swiggy' AND {oct_range}"))
    add("relative", "How much did I spend on shopping last month?", spend_sql(f"category = 'Shopping' AND {oct_range}"))
    add("relative", "How much have I spent this financial year?", spend_sql(fy_range))
    add("relative", "What's my transport spend so far this financial year?", spend_sql(f"category = 'Transport' AND {fy_range}"))
    add("relative", "How much have I invested this financial year?",
        f"SELECT SUM(amount_paise) FROM investments WHERE {fy_range}")
    add("relative", "How much did I spend in the last financial year?",
        spend_sql("txn_date >= '2025-04-01' AND txn_date < '2026-04-01'"))

    # Traps: a naive SUM over all debits gets these wrong.
    add("semantics", "What is my total spending across the whole statement period?", spend_sql("1 = 1"))
    add("semantics", "How much did I actually spend on Amazon overall, after refunds?", spend_sql("merchant = 'Amazon'"))
    add("semantics", "How much have I received in refunds overall?",
        "SELECT SUM(amount_paise) FROM all_transactions WHERE txn_type = 'refund'")
    add("semantics", "How much did I pay towards my credit card bills overall?",
        "SELECT SUM(amount_paise) FROM all_transactions WHERE txn_type = 'card_payment' AND direction = 'debit'")
    add("semantics", "How much did I move between my own accounts overall?",
        "SELECT SUM(amount_paise) FROM all_transactions WHERE txn_type = 'internal_transfer' AND direction = 'debit'")
    add("semantics", "What is my total salary received?",
        "SELECT SUM(amount_paise) FROM income_received WHERE category = 'Salary'")
    add("semantics", "How much have I put into SIPs in total?", "SELECT SUM(amount_paise) FROM investments")
    add("semantics", "How much cash did I withdraw overall?",
        "SELECT COALESCE(SUM(net_amount_paise), 0) FROM spending WHERE txn_type = 'cash_withdrawal'")
    add("semantics", "How much did I spend on my ICICI credit card in September 2026?",
        spend_sql("account = 'ICICI-CARD' AND txn_date >= '2026-09-01' AND txn_date < '2026-10-01'"))
    add("semantics", "How much have I paid in bank fees?", spend_sql("txn_type = 'fee'"))
    add("semantics", "What did I earn in interest overall?",
        "SELECT SUM(amount_paise) FROM income_received WHERE category = 'Interest'")
    add("semantics", "How much money have friends sent me?",
        "SELECT SUM(amount_paise) FROM income_received WHERE category = 'People'")

    for count, label, (year, month) in ((5, "September 2026", (2026, 9)), (3, "January 2026", (2026, 1)), (5, "June 2026", (2026, 6))):
        start, end = month_range(year, month)
        add("ranking", f"What were my top {count} merchants by spend in {label}?",
            f"SELECT merchant, SUM(net_amount_paise) AS total FROM spending WHERE txn_date >= '{start}' AND txn_date < '{end}' "
            f"GROUP BY merchant ORDER BY total DESC LIMIT {count}", kind="table", ordered=True)
    add("ranking", "Which three categories did I spend the most on overall?",
        "SELECT category, SUM(net_amount_paise) AS total FROM spending GROUP BY category ORDER BY total DESC LIMIT 3",
        kind="table", ordered=True)
    add("ranking", "What was my biggest single expense in the whole period, excluding rent?",
        "SELECT MAX(net_amount_paise) FROM spending WHERE category <> 'Rent'")
    add("ranking", "Which month did I spend the most in, and how much?",
        "SELECT substr(txn_date, 1, 7) AS month, SUM(net_amount_paise) AS total FROM spending GROUP BY month ORDER BY total DESC LIMIT 1",
        kind="table", ordered=True)

    add("trend", "Show my spending for each month.",
        "SELECT substr(txn_date, 1, 7) AS month, SUM(net_amount_paise) FROM spending GROUP BY month ORDER BY month",
        kind="table", ordered=True)
    add("trend", "Give me my month-by-month food delivery spend.",
        "SELECT substr(txn_date, 1, 7) AS month, SUM(net_amount_paise) FROM spending WHERE category = 'Food Delivery' GROUP BY month ORDER BY month",
        kind="table", ordered=True)
    add("trend", "How much did I spend per category in September 2026?",
        "SELECT category, SUM(net_amount_paise) FROM spending WHERE txn_date >= '2026-09-01' AND txn_date < '2026-10-01' GROUP BY category",
        kind="table")

    for merchant in ("Swiggy", "Uber", "Zomato"):
        add("count", f"How many times did I order or pay {merchant} in March 2026?",
            f"SELECT COUNT(*) FROM spending WHERE merchant = '{merchant}' AND txn_type = 'purchase' "
            "AND txn_date >= '2026-03-01' AND txn_date < '2026-04-01'", kind="count")
    add("count", "How many refunds did I get overall?", "SELECT COUNT(*) FROM all_transactions WHERE txn_type = 'refund'", kind="count")
    add("count", "How many ATM withdrawals have I made?", "SELECT COUNT(*) FROM spending WHERE txn_type = 'cash_withdrawal'", kind="count")

    add("subscriptions", "How much do I spend on subscriptions per month on average?",
        "SELECT SUM(net_amount_paise) / COUNT(DISTINCT substr(txn_date, 1, 7)) FROM spending WHERE category = 'Subscriptions'")
    add("subscriptions", "Which subscriptions am I paying for?",
        "SELECT DISTINCT merchant FROM spending WHERE category = 'Subscriptions'", kind="table")

    sept = "txn_date >= '2026-09-01' AND txn_date < '2026-10-01'"
    add("varied", "Did I spend more on Swiggy or Zomato overall? Give me both totals.",
        "SELECT merchant, SUM(net_amount_paise) FROM spending WHERE merchant IN ('Swiggy', 'Zomato') GROUP BY merchant", kind="table")
    add("varied", "What's my average Uber ride cost?",
        "SELECT SUM(net_amount_paise) / COUNT(*) FROM spending WHERE merchant = 'Uber'")
    add("varied", "How much do I spend in an average month?",
        "SELECT SUM(net_amount_paise) / COUNT(DISTINCT substr(txn_date, 1, 7)) FROM spending")
    add("varied", "What's my rent payment total over the period?", spend_sql("category = 'Rent'"))
    add("varied", "How much did I spend on fuel in 2026?", spend_sql("category = 'Fuel' AND txn_date >= '2026-01-01' AND txn_date < '2027-01-01'"))
    add("varied", "How much went on travel between January and March 2026?",
        spend_sql("category = 'Travel' AND txn_date >= '2026-01-01' AND txn_date < '2026-04-01'"))
    add("varied", "What did I pay Rahul Verma in total?", spend_sql("merchant = 'Rahul Verma'"))
    add("varied", "How much did Priya Nair send me overall?",
        "SELECT COALESCE(SUM(amount_paise), 0) FROM income_received WHERE merchant = 'Priya Nair'")
    add("varied", "How much did I save in the ELSS fund overall?",
        "SELECT SUM(amount_paise) FROM investments WHERE merchant LIKE '%Elss%'")
    add("varied", "What's my biggest Amazon purchase?",
        "SELECT MAX(net_amount_paise) FROM spending WHERE merchant = 'Amazon'")
    add("varied", "How much did I spend on my HDFC account in September 2026?", spend_sql(f"account = 'HDFC-SAVINGS' AND {sept}"))
    add("varied", "How many different merchants did I pay in September 2026?",
        f"SELECT COUNT(DISTINCT merchant) FROM spending WHERE {sept}", kind="count")
    add("varied", "How many transactions of any kind are in my data?", "SELECT COUNT(*) FROM all_transactions", kind="count")
    add("varied", "What's my total income, including salary, interest and money from people?",
        "SELECT SUM(amount_paise) FROM income_received")
    add("varied", "How much did I spend on health overall?", spend_sql("category = 'Health'"))
    add("varied", "Which merchant did I pay the most often in the whole period?",
        "SELECT merchant, COUNT(*) AS n FROM spending WHERE txn_type = 'purchase' GROUP BY merchant ORDER BY n DESC LIMIT 1",
        kind="table", ordered=True)
    add("varied", "What's the most I spent on food delivery in any one month?",
        "SELECT MAX(total) FROM (SELECT SUM(net_amount_paise) AS total FROM spending WHERE category = 'Food Delivery' GROUP BY substr(txn_date, 1, 7))")
    add("varied", "How much did I spend in the three months to the end of October 2026?",
        spend_sql("txn_date >= '2026-08-01' AND txn_date < '2026-11-01'"))
    add("varied", "What did I spend on Netflix overall?", spend_sql("merchant = 'Netflix'"))
    add("varied", "How much do I pay Airtel each month on average?",
        "SELECT SUM(net_amount_paise) / COUNT(DISTINCT substr(txn_date, 1, 7)) FROM spending WHERE merchant = 'Airtel'")

    # No matching data: the right answer is zero, not an invented figure.
    add("zero", "How much did I spend on Ferrari last year?", spend_sql("merchant = 'Ferrari'"))
    add("zero", "What did I spend on Swiggy in December 2020?",
        spend_sql("merchant = 'Swiggy' AND txn_date >= '2020-12-01' AND txn_date < '2021-01-01'"))

    # Not answerable from these views: the agent must decline to give a figure.
    for question in ("What is the current value of my mutual fund portfolio?",
                     "What's my current bank balance?",
                     "What's my credit score?",
                     "How much will I spend next year?"):
        add("unanswerable", question, None, kind="abstain")

    for index, question in enumerate(questions):
        digest = int(hashlib.sha256(question["question"].encode()).hexdigest(), 16)
        question["id"] = f"q{index:03d}"
        question["split"] = "test" if digest % 100 < TEST_SHARE_PERCENT else "dev"
    return questions


def main() -> None:
    questions = build()
    out = Path(__file__).with_name("questions.jsonl")
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        for question in questions:
            handle.write(json.dumps(question, ensure_ascii=False) + "\n")
    test_count = sum(q["split"] == "test" for q in questions)
    print(f"Wrote {len(questions)} questions to {out} ({len(questions) - test_count} dev, {test_count} test)")


if __name__ == "__main__":
    main()
