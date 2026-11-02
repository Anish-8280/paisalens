"""Score the agent against the benchmark.

  python eval/run_eval.py --mode gold              Check gold SQL runs. Free, needs no API key.
  python eval/run_eval.py --mode agent --split dev Call Claude on each question. Costs money.

Two scores per question: `sql_correct` (did a query return the right values) and `answer_correct`
(does the prose quote them). An answer can have correct SQL and still misreport the number.
"""
import argparse
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.loop import Settings, ask  # noqa: E402
from app.sql.guardrails import run_query  # noqa: E402
from eval.build_questions import AS_OF  # noqa: E402

QUESTIONS_PATH = Path(__file__).with_name("questions.jsonl")
RESULTS_DIR = Path(__file__).with_name("results")
ROUND_TOLERANCE_PAISE = 100  # a whole-rupee mention of a figure still counts as quoting it


def load_questions(split: str) -> list[dict]:
    questions = [json.loads(line) for line in QUESTIONS_PATH.read_text("utf-8").splitlines()]
    return [q for q in questions if split == "all" or q["split"] == split]


def normalise(value):
    if value is None:
        return 0
    if isinstance(value, float):
        return round(value)
    return value


def row_contains(row: tuple, gold_row: tuple) -> bool:
    """True if every gold value appears in the row. Extra columns are fine, such as a name beside a total."""
    remaining = [normalise(value) for value in row]
    for value in gold_row:
        value = normalise(value)
        if value not in remaining:
            return False
        remaining.remove(value)
    return True


def result_matches(rows: list[tuple], gold: list[tuple], ordered: bool) -> bool:
    if len(rows) != len(gold):
        return False
    if ordered:
        return all(row_contains(row, gold_row) for row, gold_row in zip(rows, gold))
    unmatched = list(rows)
    for gold_row in gold:
        match = next((row for row in unmatched if row_contains(row, gold_row)), None)
        if match is None:
            return False
        unmatched.remove(match)
    return True


def numbers_in(text: str) -> list[float]:
    return [float(match.replace(",", "")) for match in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def answer_quotes(text: str, gold: list[tuple], kind: str) -> bool:
    """Check the prose carries the gold figure(s) and names. Rupee figures may be rounded to the rupee."""
    numbers = numbers_in(text)
    for row in gold:
        for value in row:
            if isinstance(value, str):
                if value.lower() not in text.lower() and not re.fullmatch(r"\d{4}-\d{2}", value):
                    return False
            elif value in (None, 0):
                continue  # a zero can be phrased as "no transactions"
            elif kind == "count":
                if float(value) not in numbers:
                    return False
            elif not any(abs(number * 100 - value) <= ROUND_TOLERANCE_PAISE for number in numbers):
                return False
    return True


def grade(question: dict, answer, gold: list[tuple]) -> dict:
    if question["kind"] == "abstain":
        # Declining means quoting no rupee figure. Running exploratory SQL along the way is fine.
        declined = answer.status == "ok" and "₹" not in answer.text
        return {"sql_correct": declined, "answer_correct": declined}
    sql_correct = any(result_matches(result.rows, gold, question["ordered"]) for result in answer.results)
    return {"sql_correct": sql_correct, "answer_correct": sql_correct and answer_quotes(answer.text, gold, question["kind"])}


def run_gold(questions: list[dict], settings: Settings) -> int:
    failures = 0
    for question in questions:
        if question["gold_sql"] is None:
            continue
        try:
            result = run_query(settings.database_path, question["gold_sql"], settings.row_limit)
            if not result.rows or result.truncated:
                raise ValueError("no rows" if not result.rows else "truncated")
        except ValueError as exc:
            failures += 1
            print(f"FAIL {question['id']} {question['question']}: {exc}")
    print(f"Gold check: {len(questions) - failures}/{len(questions)} questions OK")
    return failures


def run_agent(questions: list[dict], settings: Settings) -> None:
    records = []
    by_group = defaultdict(lambda: [0, 0, 0])
    for question in questions:
        gold = run_query(settings.database_path, question["gold_sql"], settings.row_limit).rows if question["gold_sql"] else []
        started = time.perf_counter()
        answer = ask(question["question"], settings)
        scores = grade(question, answer, gold)
        records.append({
            "id": question["id"], "group": question["group"], "split": question["split"],
            "question": question["question"], "answer": answer.text, "status": answer.status, "sql": answer.sql,
            "seconds": round(time.perf_counter() - started, 2), "steps": answer.steps,
            "input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens,
            "cache_read_tokens": answer.cache_read_tokens, "cache_write_tokens": answer.cache_write_tokens, **scores,
        })
        tally = by_group[question["group"]]
        tally[0] += 1
        tally[1] += scores["sql_correct"]
        tally[2] += scores["answer_correct"]
        print(f"{question['id']} sql={'Y' if scores['sql_correct'] else 'N'} answer={'Y' if scores['answer_correct'] else 'N'} {question['question']}", flush=True)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{settings.model}.jsonl"
    out.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), "utf-8")

    print(f"\n{'group':<18}{'n':>4}{'sql':>6}{'answer':>8}")
    for group, (count, sql_ok, answer_ok) in sorted(by_group.items()):
        print(f"{group:<18}{count:>4}{sql_ok:>6}{answer_ok:>8}")
    total = len(records)
    seconds = sorted(record["seconds"] for record in records)
    print(f"\nSQL correct: {sum(r['sql_correct'] for r in records)}/{total}")
    print(f"Answer correct: {sum(r['answer_correct'] for r in records)}/{total}")
    print(f"p50 {seconds[total // 2]}s, p95 {seconds[min(total - 1, int(total * 0.95))]}s")
    print(f"Tokens: {sum(r['input_tokens'] for r in records)} in, {sum(r['output_tokens'] for r in records)} out, "
          f"{sum(r['cache_read_tokens'] for r in records)} cache read, {sum(r['cache_write_tokens'] for r in records)} cache write")
    print(f"Results: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["gold", "agent"], default="gold")
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--limit", type=int, help="Only run the first N questions")
    args = parser.parse_args()

    settings = replace(Settings.from_env(), as_of=AS_OF)
    questions = load_questions(args.split)[:args.limit]
    if args.mode == "gold":
        sys.exit(1 if run_gold(questions, settings) else 0)
    run_agent(questions, settings)


if __name__ == "__main__":
    main()
