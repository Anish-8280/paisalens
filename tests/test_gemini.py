"""The Gemini loop against a scripted transport. No API calls."""
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from app.agent import gemini
from app.agent.gemini import GeminiError, default_transport
from app.agent.loop import Settings, ask
from app.limits import UsageLimiter
from app.main import create_app

USAGE = {"promptTokenCount": 100, "candidatesTokenCount": 10, "thoughtsTokenCount": 5, "cachedContentTokenCount": 40}


def reply(parts, finish="STOP"):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}], "usageMetadata": USAGE}


def call_tool(sql, **extra):
    return reply([{"functionCall": {"name": "query_transactions", "args": {"sql": sql}}, **extra}])


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, model, body):
        self.requests.append((model, json.loads(json.dumps(body))))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def settings(db_path):
    return Settings(database_path=str(db_path), model="gemini-flash-lite-latest", as_of="2026-11-02", max_steps=3)


def test_runs_sql_and_returns_the_answer(settings):
    transport = Transport(call_tool("SELECT SUM(net_amount_paise) AS total_paise FROM spending"), reply([{"text": "You spent a lot."}]))
    answer = ask("How much?", settings, transport)
    assert answer.status == "ok" and answer.text == "You spent a lot." and answer.steps == 2
    assert len(answer.results) == 1
    assert (answer.input_tokens, answer.output_tokens, answer.cache_read_tokens) == (120, 30, 80)
    result = transport.requests[1][1]["contents"][-1]["parts"][0]["functionResponse"]
    assert result["name"] == "query_transactions" and "total_rupees" in result["response"]["result"]


def test_model_parts_are_sent_back_unchanged_with_their_signatures(settings):
    transport = Transport(call_tool("SELECT 1", thoughtSignature="opaque-signature"), reply([{"text": "done"}]))
    ask("How much?", settings, transport)
    assert transport.requests[1][1]["contents"][1]["parts"][0]["thoughtSignature"] == "opaque-signature"


def test_rejected_sql_goes_back_as_an_error(settings):
    transport = Transport(call_tool("DELETE FROM transactions"), reply([{"text": "Sorry."}]))
    answer = ask("Delete everything", settings, transport)
    response = transport.requests[1][1]["contents"][-1]["parts"][0]["functionResponse"]["response"]
    assert "SELECT" in response["error"] and answer.results == []


def test_step_limit_gives_no_figure(settings):
    answer = ask("Loop", settings, Transport(*[call_tool("SELECT 1") for _ in range(3)]))
    assert answer.status == "step_limit" and "₹" not in answer.text


def test_thought_text_is_not_part_of_the_answer(settings):
    answer = ask("x", settings, Transport(reply([{"text": "secret plan", "thought": True}, {"text": "The answer."}])))
    assert answer.text == "The answer."


@pytest.mark.parametrize("response", [{"promptFeedback": {"blockReason": "SAFETY"}}, reply([], finish="SAFETY")])
def test_blocked_responses_are_refusals(settings, response):
    assert ask("x", settings, Transport(response)).status == "refused"


def test_cut_off_response_is_reported(settings):
    assert ask("x", settings, Transport(reply([{"text": "par"}], finish="MAX_TOKENS"))).status == "max_tokens"


def test_gemma_models_use_this_loop_too(settings):
    settings.model = "gemma-4-26b-a4b-it"
    transport = Transport(reply([{"text": "ok"}]))
    ask("x", settings, transport)
    assert transport.requests[0][0] == "gemma-4-26b-a4b-it"


def http_error(status):
    return urllib.error.HTTPError("https://example.test", status, "reason", {}, None)


def test_temporary_failures_retry_but_quota_errors_do_not(monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda _: None)
    calls = []

    def fail_then_work(request, timeout):
        calls.append(request)
        raise http_error(503)

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fail_then_work)
    with pytest.raises(GeminiError):
        default_transport("m", {})
    assert len(calls) == gemini.MAX_ATTEMPTS

    calls.clear()

    def quota(request, timeout):
        calls.append(request)
        raise http_error(429)

    monkeypatch.setattr(gemini.urllib.request, "urlopen", quota)
    with pytest.raises(GeminiError) as caught:
        default_transport("m", {})
    assert len(calls) == 1 and caught.value.temporary


def test_key_travels_in_a_header_and_not_the_url(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-value")
    seen = {}

    def capture(request, timeout):
        seen["url"], seen["key"] = request.full_url, request.get_header("X-goog-api-key")
        raise http_error(400)

    monkeypatch.setattr(gemini.urllib.request, "urlopen", capture)
    with pytest.raises(GeminiError):
        default_transport("m", {})
    assert seen["key"] == "test-value" and "test-value" not in seen["url"]


@pytest.mark.parametrize("error, status", [(GeminiError(429), 503), (GeminiError(503), 503), (GeminiError(0), 503),
                                           (GeminiError(400, "secret"), 502), (GeminiError(403, "secret"), 502)])
def test_service_maps_gemini_errors_without_leaking_details(settings, error, status):
    app = create_app(settings, Transport(error), UsageLimiter(100, 100))
    response = TestClient(app, raise_server_exceptions=False).post("/ask", json={"question": "a"})
    assert response.status_code == status and "secret" not in response.text


def test_service_needs_the_gemini_key_for_gemini_models(settings, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated")
    client = TestClient(create_app(settings))
    assert client.get("/health").json()["model_configured"] is False
    assert client.post("/ask", json={"question": "a"}).status_code == 503
