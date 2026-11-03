"""Generate a deterministic synthetic statement, including the cases that break naive totals:
own-account transfers, credit card bill payments, refunds, reversals, SIPs and P2P payments.

Writes two files. The CSV is what ingestion reads. The truth file records each row's real type
and category, so tests can check classification and totals without going through the parser.

Run: python scripts/generate_synthetic_data.py --out data/sample/transactions.csv
"""
import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

SAVINGS = "HDFC-SAVINGS"
SECOND = "SBI-SAVINGS"
CARD = "ICICI-CARD"

# (payee as printed, category, min rupees, max rupees, purchases per month)
UPI_MERCHANTS = [
    ("SWIGGY LTD", "Food Delivery", 180, 950, 12),
    ("ZOMATO LIMITED", "Food Delivery", 150, 900, 9),
    ("SWIGGY INSTAMART", "Groceries", 250, 1400, 6),
    ("BLINKIT", "Groceries", 150, 1100, 6),
    ("SHARMA KIRANA STORE", "Other", 60, 700, 14),
    ("BLUE TOKAI CAFE", "Other", 180, 650, 7),
    ("UBER INDIA", "Transport", 90, 620, 16),
    ("OLA CABS", "Transport", 80, 540, 8),
    ("HPCL PETROL PUMP", "Fuel", 500, 2800, 3),
    ("APOLLO PHARMACY", "Health", 120, 1800, 2),
]
CARD_MERCHANTS = [
    ("AMAZON SELLER SERVICES", "Shopping", 300, 6500, 8),
    ("FLIPKART INTERNET", "Shopping", 350, 5200, 4),
    ("MYNTRA DESIGNS", "Shopping", 600, 4200, 2),
    ("BIGBASKET", "Groceries", 500, 2600, 4),
    ("INDIGO AIRLINES", "Travel", 3500, 14000, 1),
    ("IRCTC", "Travel", 400, 3200, 1),
]
FRIENDS = ["RAHUL VERMA", "PRIYA NAIR", "KARAN SINGH", "NEHA GUPTA"]
SUBSCRIPTIONS = [(SAVINGS, "ACH/NETFLIX ENTERTAINMENT/SUB", 649), (CARD, "CARD/SPOTIFY INDIA", 119), (CARD, "CARD/HOTSTAR", 299)]
BILLS = [("AIRTEL BROADBAND", 1179, 1179), ("BESCOM ELECTRICITY", 900, 2600), ("JIO PREPAID", 299, 299)]
SIPS = [("HDFCMF SIP ELSS TAX SAVER", 5000), ("PPFAS MF SIP FLEXI CAP", 10000)]
SALARY_RUPEES = 145000
RENT_RUPEES = 28000


def month_starts(start: date, months: int):
    year, month = start.year, start.month
    for _ in range(months):
        yield date(year, month, 1)
        month += 1
        if month == 13:
            year, month = year + 1, 1


def days_in_month(first: date) -> int:
    return ((first.replace(day=28) + timedelta(days=4)).replace(day=1) - first).days


def generate(start: date, months: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    final_month = list(month_starts(start, months))[-1]
    last_date = final_month.replace(day=days_in_month(final_month))

    def add(day, account, narration, rupees, direction, txn_type, category, merchant):
        paise = round(rupees * 100)
        rows.append({
            "date": min(day, last_date), "account": account, "narration": narration,
            "debit": paise if direction == "debit" else 0,
            "credit": paise if direction == "credit" else 0,
            "txn_type": txn_type, "category": category, "merchant": merchant,
        })

    def ref():
        return rng.randint(100000000000, 999999999999)

    for first in month_starts(start, months):
        last_day = days_in_month(first)
        pick_day = lambda: first.replace(day=rng.randint(1, last_day))
        card_total = 0.0

        add(first.replace(day=1), SAVINGS, f"NEFT/N{ref()}/SALARY/ACME TECHNOLOGIES PVT LTD", SALARY_RUPEES,
            "credit", "income", "Salary", "Acme Technologies")
        add(first.replace(day=3), SAVINGS, f"UPI/{ref()}/PRESTIGE ESTATES/rent@okhdfc/Rent", RENT_RUPEES,
            "debit", "purchase", "Rent", "Rent")

        for payee, category, low, high, count in UPI_MERCHANTS:
            for _ in range(rng.randint(max(1, count - 2), count + 2)):
                rupees = round(rng.uniform(low, high), 2)
                handle = payee.split()[0].lower() + "@axisbank"
                add(pick_day(), SAVINGS, f"UPI/{ref()}/{payee}/{handle}/Payment", rupees,
                    "debit", "purchase", category, None)

        for payee, category, low, high, count in CARD_MERCHANTS:
            for _ in range(rng.randint(0 if count == 1 else max(1, count - 1), count + 1)):
                rupees = round(rng.uniform(low, high), 2)
                day = pick_day()
                add(day, CARD, f"POS/{ref()}/{payee}", rupees, "debit", "purchase", category, None)
                card_total += rupees
                if rng.random() < 0.12:  # refund, always later and sometimes partial
                    refund = round(rupees * rng.choice([1, 1, 0.5]), 2)
                    add(day + timedelta(days=rng.randint(2, 9)), CARD, f"REFUND/{ref()}/{payee}", refund,
                        "credit", "refund", category, None)
                    card_total -= refund

        for account, narration, rupees in SUBSCRIPTIONS:
            add(first.replace(day=5), account, narration, rupees, "debit", "purchase", "Subscriptions", None)
            if account == CARD:
                card_total += rupees

        for payee, low, high in BILLS:
            add(first.replace(day=7), SAVINGS, f"UPI/{ref()}/{payee}/bills@okaxis/Bill",
                round(rng.uniform(low, high), 2), "debit", "purchase", "Bills", None)

        for scheme, rupees in SIPS:
            add(first.replace(day=10), SAVINGS, f"ACH/{scheme}/{ref()}", rupees, "debit", "investment", "Investments", None)

        for _ in range(rng.randint(2, 5)):
            friend = rng.choice(FRIENDS)
            handle = friend.split()[0].lower() + "@oksbi"
            rupees = rng.choice([200, 350, 500, 750, 1000, 2000])
            direction = rng.choice(["debit", "debit", "credit"])
            add(pick_day(), SAVINGS, f"UPI/{ref()}/{friend}/{handle}/Split", rupees, direction, "p2p", "People", None)

        # A failed UPI payment that is reversed: nets to zero.
        if rng.random() < 0.4:
            rupees = round(rng.uniform(150, 800), 2)
            day = pick_day()
            add(day, SAVINGS, f"UPI/{ref()}/ZOMATO LIMITED/zomato@axisbank/Payment", rupees,
                "debit", "purchase", "Food Delivery", None)
            add(day + timedelta(days=1), SAVINGS, f"UPI/REV/{ref()}/ZOMATO LIMITED", rupees,
                "credit", "refund", "Food Delivery", None)

        if rng.random() < 0.7:
            add(pick_day(), SAVINGS, f"ATM/{ref()}/MG ROAD BENGALURU", rng.choice([2000, 3000, 5000]),
                "debit", "cash_withdrawal", "Cash", "ATM")
        if first.month % 3 == 0:
            add(first.replace(day=last_day), SAVINGS, "CHARGE/SMS ALERT FEE QTR", 17.70, "debit", "fee", "Fees", "Bank Charges")
            add(first.replace(day=last_day), SAVINGS, "INTEREST/CREDIT QTR", round(rng.uniform(900, 1400), 2),
                "credit", "income", "Interest", "Interest")

        # Own-account transfer, printed on both accounts. Must not count as spending or income.
        rupees = rng.choice([10000, 15000, 20000])
        day = first.replace(day=15)
        ref_no = ref()
        add(day, SAVINGS, f"IMPS/{ref_no}/TRANSFER/ARJUN MEHTA", rupees, "debit", "internal_transfer", "Transfers", "Own Account")
        add(day, SECOND, f"IMPS/{ref_no}/TRANSFER/ARJUN MEHTA", rupees, "credit", "internal_transfer", "Transfers", "Own Account")

        # Card bill, also printed on both accounts. The purchases above are the real spending.
        if card_total > 0:
            amount = round(card_total, 2)
            pay_day = first.replace(day=last_day)
            add(pay_day, SAVINGS, f"UPI/{ref()}/CC PAYMENT ICICI CARD/icici@upi/Bill", amount, "debit", "card_payment", "Transfers", "Credit Card Payment")
            add(pay_day, CARD, "PAYMENT RECEIVED THANK YOU", amount, "credit", "card_payment", "Transfers", "Credit Card Payment")

    rows.sort(key=lambda row: (row["date"], row["account"], row["narration"]))
    return rows


def rupees_text(paise: int) -> str:
    return f"{paise / 100:.2f}" if paise else ""


def write(rows: list[dict], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "account", "narration", "debit", "credit"])
        for row in rows:
            writer.writerow([row["date"].strftime("%d/%m/%Y"), row["account"], row["narration"],
                             rupees_text(row["debit"]), rupees_text(row["credit"])])

    truth_path = out.with_name(out.stem + "_truth.csv")
    with open(truth_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "account", "narration", "debit_paise", "credit_paise", "txn_type", "category"])
        for row in rows:
            writer.writerow([row["date"].isoformat(), row["account"], row["narration"],
                             row["debit"], row["credit"], row["txn_type"], row["category"]])
    return truth_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-11-01", type=date.fromisoformat)
    parser.add_argument("--months", default=12, type=int)
    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--out", default="data/sample/transactions.csv", type=Path)
    args = parser.parse_args()

    rows = generate(args.start, args.months, args.seed)
    truth_path = write(rows, args.out)
    print(f"Wrote {len(rows)} rows to {args.out} and {truth_path}")


if __name__ == "__main__":
    main()
