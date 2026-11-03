"""The same bounded agent loop as app.agent.loop, for Gemini and Gemma models over the REST API."""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date

from app.agent.loop import Answer, Settings, execute_query
from app.agent.prompts import SYSTEM_PROMPT, user_turn
from app.agent.tools import QUERY_TOOL

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_OUTPUT_TOKENS = 8192
REQUEST_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
RETRYABLE_STATUSES = {500, 503}  # not 429: retrying would only spend more of a small free quota
BLOCKED_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}

TOOL_DECLARATION = {"functionDeclarations": [{
    "name": QUERY_TOOL["name"],
    "description": QUERY_TOOL["description"],
    "parameters": {
        "type": "OBJECT",
        "properties": {"sql": {"type": "STRING", "description": "A single SELECT statement."}},
        "required": ["sql"],
    },
}]}


class GeminiError(Exception):
    """A failed API call. `status` is the HTTP code, or 0 if the service couldn't be reached."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(f"Gemini API error {status}: {message}")
        self.status = status
        self.temporary = status == 0 or status == 429 or status in RETRYABLE_STATUSES


def _send(model: str, body: dict) -> dict:
    # The key goes in a header because URLs end up in logs.
    request = urllib.request.Request(f"{API_BASE}/{model}:generateContent", data=json.dumps(body).encode(),
                                     headers={"x-goog-api-key": os.environ.get("GEMINI_API_KEY", ""),
                                              "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise GeminiError(exc.code, exc.reason) from exc
    except OSError as exc:
        raise GeminiError(0, type(exc).__name__) from exc


def default_transport(model: str, body: dict) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _send(model, body)
        except GeminiError as exc:
            if attempt == MAX_ATTEMPTS or not (exc.status == 0 or exc.status in RETRYABLE_STATUSES):
                raise
            time.sleep(RETRY_DELAY_SECONDS * attempt)


def _text_of(parts: list[dict]) -> str:
    return "\n".join(part["text"] for part in parts if "text" in part and not part.get("thought")).strip()


def ask(question: str, settings: Settings, transport=None, history: list[tuple[str, str]] = ()) -> Answer:
    transport = transport or default_transport
    as_of = settings.as_of or date.today().isoformat()
    contents = []
    for earlier_question, earlier_answer in history:
        contents.append({"role": "user", "parts": [{"text": earlier_question}]})
        contents.append({"role": "model", "parts": [{"text": earlier_answer}]})
    contents.append({"role": "user", "parts": [{"text": user_turn(question, as_of)}]})
    answer = Answer(text="")

    while answer.steps < settings.max_steps:
        answer.steps += 1
        response = transport(settings.model, {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": contents,
            "tools": [TOOL_DECLARATION],
            "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
        })
        usage = response.get("usageMetadata", {})
        cached = usage.get("cachedContentTokenCount", 0)
        answer.input_tokens += usage.get("promptTokenCount", 0) - cached
        answer.output_tokens += usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        answer.cache_read_tokens += cached

        candidates = response.get("candidates") or []
        if not candidates:
            answer.text, answer.status = "I can't help with that request.", "refused"
            return answer
        candidate = candidates[0]
        finish = candidate.get("finishReason", "STOP")
        parts = candidate.get("content", {}).get("parts") or []
        if finish in BLOCKED_REASONS:
            answer.text, answer.status = "I can't help with that request.", "refused"
            return answer
        if finish == "MAX_TOKENS":
            answer.text, answer.status = _text_of(parts), "max_tokens"
            return answer

        calls = [part["functionCall"] for part in parts if "functionCall" in part]
        if not calls:
            answer.text = _text_of(parts)
            return answer

        # Sent back unchanged, because newer models attach signatures to these parts and reject edits.
        contents.append(candidate["content"])
        results = []
        for call in calls:
            text, is_error = execute_query(call.get("args", {}).get("sql", ""), settings, answer)
            result = {"name": call["name"], "response": {"error" if is_error else "result": text}}
            if "id" in call:
                result["id"] = call["id"]
            results.append({"functionResponse": result})
        contents.append({"role": "user", "parts": results})

    answer.text = "I couldn't finish working this out within the step limit, so I'm not giving a number."
    answer.status = "step_limit"
    return answer
