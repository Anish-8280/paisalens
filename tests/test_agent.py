"""The agent loop against a scripted fake client. No API calls, so this checks the plumbing and not Claude."""
import json
from types import SimpleNamespace as NS

import pytest

from app.agent.loop import Settings, ask
from app.agent.tools import format_inr
from eval.run_eval import answer_quotes, load_questions, result_matches


def usage():
    return NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def text(value, stop="end_turn"):
    return NS(stop_reason=stop, usage=usage(), content=[NS(type="text", text=value)])


def tool(sql, call_id="t1"):
    return NS(stop_reason="tool_use", usage=usage(),
              content=[NS(type="tool_use", id=call_id, name="query_transactions", input={"sql": sql})])


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


@pytest.fixture
def settings(db_path):
    return Settings(database_path=str(db_path), as_of="2026-11-02", max_steps=3)


def tool_result_of(request):
    return request["messages"][-1]["content"][0]


def test_runs_sql_and_returns_the_answer(settings):
    client = FakeClient(tool("SELECT SUM(net_amount_paise) AS total_paise FROM spending"), text("You spent a lot."))
    answer = ask("How much?", settings, client)
    assert answer.status == "ok" and answer.text == "You spent a lot."
    assert len(answer.results) == 1 and answer.steps == 2 and answer.input_tokens == 20
    payload = json.loads(tool_result_of(client.requests[1])["content"])
    assert payload["rows"][0]["total_rupees"].startswith("₹")


def test_pinned_date_is_in_the_user_turn_and_not_the_system_prompt(settings):
    client = FakeClient(text("ok"))
    ask("How much?", settings, client)
    assert "2026-11-02" in client.requests[0]["messages"][0]["content"]
    assert "2026-11-02" not in client.requests[0]["system"][0]["text"]


def test_rejected_sql_is_returned_to_the_model_as_an_error(settings):
    client = FakeClient(tool("DELETE FROM transactions"), text("Sorry."))
    answer = ask("Delete everything", settings, client)
    result = tool_result_of(client.requests[1])
    assert result["is_error"] is True and "SELECT" in result["content"]
    assert answer.results == []


def test_step_limit_stops_the_loop_without_a_figure(settings):
    client = FakeClient(*[tool("SELECT 1") for _ in range(3)])
    answer = ask("Loop forever", settings, client)
    assert answer.status == "step_limit" and answer.steps == 3 and "₹" not in answer.text


def test_refusal_is_reported(settings):
    answer = ask("x", settings, FakeClient(text("", stop="refusal")))
    assert answer.status == "refused"


@pytest.mark.parametrize("paise, expected", [
    (0, "₹0.00"), (5, "₹0.05"), (123450, "₹1,234.50"), (12345678, "₹1,23,456.78"),
    (1234567890, "₹1,23,45,678.90"), (-5000, "-₹50.00"), (99999, "₹999.99"),
])
def test_format_inr(paise, expected):
    assert format_inr(paise) == expected


def test_grader_accepts_extra_columns_and_rejects_wrong_values():
    gold = [("Swiggy", 500), ("Uber", 300)]
    assert result_matches([("Swiggy", 500, 3), ("Uber", 300, 1)], gold, ordered=True)
    assert not result_matches([("Uber", 300), ("Swiggy", 500)], gold, ordered=True)
    assert result_matches([("Uber", 300), ("Swiggy", 500)], gold, ordered=False)
    assert not result_matches([("Swiggy", 501), ("Uber", 300)], gold, ordered=True)
    assert result_matches([(None,)], [(0,)], ordered=False)


def test_grader_checks_the_prose_not_just_the_sql():
    assert answer_quotes("You spent ₹1,23,456.78 on food.", [(12345678,)], "money")
    assert answer_quotes("About ₹1,23,457.", [(12345678,)], "money")
    assert not answer_quotes("You spent ₹1,20,000.", [(12345678,)], "money")
    assert answer_quotes("You have no such transactions.", [(0,)], "money")
    assert answer_quotes("Swiggy came first at ₹5.00.", [("Swiggy", 500)], "money")
    assert not answer_quotes("Zomato came first at ₹5.00.", [("Swiggy", 500)], "money")


def test_every_gold_query_passes_the_guardrails_and_returns_rows(db_path):
    from app.sql.guardrails import run_query
    questions = load_questions("all")
    assert len(questions) >= 100
    assert len({q["question"] for q in questions}) == len(questions)
    assert {q["split"] for q in questions} == {"dev", "test"}
    for question in questions:
        if question["gold_sql"]:
            assert run_query(db_path, question["gold_sql"]).rows, question["question"]
