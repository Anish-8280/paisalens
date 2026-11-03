"""A bounded Claude tool-use loop.

Run: python -m app.agent.loop "How much did I spend on Swiggy last month?"
"""
import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import date

import anthropic
from dotenv import load_dotenv

from app.agent.prompts import SYSTEM_PROMPT, user_turn
from app.agent.tools import QUERY_TOOL, run_tool
from app.sql.guardrails import QueryResult

DEFAULT_MODEL = "claude-haiku-5-5"
MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Refusal fallbacks are model-specific: Haiku has none, so it must not be sent the setting.
FALLBACK_MODELS = frozenset({"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"})


def _fallback_params(model: str) -> dict:
    return {"betas": [FALLBACK_BETA], "fallbacks": "default"} if model in FALLBACK_MODELS else {}


@dataclass
class Settings:
    database_path: str = "data/paisalens.db"
    model: str = DEFAULT_MODEL
    max_steps: int = 8
    row_limit: int = 500
    timeout_seconds: float = 5.0
    as_of: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            database_path=os.getenv("DATABASE_PATH", cls.database_path),
            model=os.getenv("MODEL", DEFAULT_MODEL),
            max_steps=int(os.getenv("AGENT_MAX_STEPS", cls.max_steps)),
            row_limit=int(os.getenv("SQL_ROW_LIMIT", cls.row_limit)),
            timeout_seconds=float(os.getenv("SQL_TIMEOUT_SECONDS", cls.timeout_seconds)),
            as_of=os.getenv("AS_OF_DATE", ""),
        )


@dataclass
class Answer:
    text: str
    status: str = "ok"  # ok | refused | step_limit | max_tokens
    sql: list[str] = field(default_factory=list)
    results: list[QueryResult] = field(default_factory=list)
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def _text_of(response) -> str:
    return "\n".join(block.text for block in response.content if block.type == "text").strip()


def is_gemini_model(model: str) -> bool:
    return model.startswith(("gemini", "gemma"))


def execute_query(sql: str, settings: Settings, answer: Answer) -> tuple[str, bool]:
    """Run one model-written query and record it. Returns (text for the model, whether it was rejected)."""
    text, result = run_tool(settings.database_path, sql, settings.row_limit, settings.timeout_seconds)
    answer.sql.append(sql)
    if result is not None:
        answer.results.append(result)
    return text, result is None


def ask(question: str, settings: Settings, client=None) -> Answer:
    if is_gemini_model(settings.model):
        from app.agent import gemini  # imported here because gemini.py builds on this module
        return gemini.ask(question, settings, client)

    client = client or anthropic.Anthropic()
    as_of = settings.as_of or date.today().isoformat()
    messages = [{"role": "user", "content": user_turn(question, as_of)}]
    answer = Answer(text="")

    while answer.steps < settings.max_steps:
        answer.steps += 1
        response = client.beta.messages.create(
            model=settings.model,
            max_tokens=MAX_TOKENS,
            output_config={"effort": "medium"},
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[QUERY_TOOL],
            messages=messages,
            **_fallback_params(settings.model),
        )
        usage = response.usage
        answer.input_tokens += usage.input_tokens
        answer.output_tokens += usage.output_tokens
        answer.cache_read_tokens += usage.cache_read_input_tokens or 0
        answer.cache_write_tokens += usage.cache_creation_input_tokens or 0

        if response.stop_reason == "refusal":
            answer.text, answer.status = "I can't help with that request.", "refused"
            return answer
        if response.stop_reason == "max_tokens":
            answer.text, answer.status = _text_of(response), "max_tokens"
            return answer

        tool_calls = [block for block in response.content if block.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_calls:
            answer.text = _text_of(response)
            return answer

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for call in tool_calls:
            text, is_error = execute_query(call.input.get("sql", ""), settings, answer)
            tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": text,
                                 "is_error": is_error})
        messages.append({"role": "user", "content": tool_results})

    answer.text = "I couldn't finish working this out within the step limit, so I'm not giving a number."
    answer.status = "step_limit"
    return answer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a code page without ₹
    answer = ask(args.question, Settings.from_env())
    print(answer.text)
    for sql in answer.sql:
        print(f"\nSQL: {sql}")
    print(f"\n[{answer.status}, {answer.steps} steps, {answer.input_tokens} in, {answer.output_tokens} out, "
          f"{answer.cache_read_tokens} cache read]")


if __name__ == "__main__":
    main()
