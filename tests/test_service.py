"""HTTP behaviour of the service, using a scripted model client. No API calls."""
from datetime import date
from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.loop import Settings
from app.limits import UsageLimiter
from app.main import create_app
from tests.test_agent import FakeClient, text, tool


def client_for(db_path, *responses, limiter=None, database=None):
    settings = Settings(database_path=str(database or db_path), as_of="2026-11-02")
    app = create_app(settings, FakeClient(*responses), limiter or UsageLimiter(100, 1000))
    return TestClient(app, raise_server_exceptions=False)


def test_ask_returns_answer_sql_and_usage(db_path):
    sql = "SELECT SUM(net_amount_paise) AS total_paise FROM spending"
    response = client_for(db_path, tool(sql), text("You spent a lot.")).post("/ask", json={"question": "How much?"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "You spent a lot." and body["status"] == "ok"
    assert body["sql"] == [sql] and body["usage"]["input_tokens"] == 20


@pytest.mark.parametrize("payload", [{"question": ""}, {"question": "   "}, {"question": "x" * 501}, {}, {"question": 5}])
def test_invalid_questions_are_rejected_without_calling_the_model(db_path, payload):
    assert client_for(db_path).post("/ask", json=payload).status_code == 422


def test_health_reports_the_database_and_never_calls_the_model(db_path):
    response = client_for(db_path).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok" and response.json()["transactions"] > 0


def test_health_fails_when_the_database_is_missing(db_path, tmp_path):
    assert client_for(db_path, database=tmp_path / "missing.db").get("/health").status_code == 503


def test_missing_credentials_return_503_but_health_stays_up(db_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    client = TestClient(create_app(Settings(database_path=str(db_path))))
    assert client.post("/ask", json={"question": "How much?"}).status_code == 503
    assert client.get("/health").json()["model_configured"] is False


def test_per_client_limit_returns_429_with_retry_after(db_path):
    client = client_for(db_path, text("one"), text("two"), limiter=UsageLimiter(1, 100))
    assert client.post("/ask", json={"question": "a"}).status_code == 200
    response = client.post("/ask", json={"question": "b"})
    assert response.status_code == 429 and response.headers["retry-after"] == "60"


def test_daily_limit_returns_429(db_path):
    client = client_for(db_path, text("one"), limiter=UsageLimiter(100, 1))
    assert client.post("/ask", json={"question": "a"}).status_code == 200
    assert "daily" in client.post("/ask", json={"question": "b"}).json()["detail"]


def api_error(status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(status, request=request, json={"error": {"message": "secret upstream detail"}})


@pytest.mark.parametrize("error, status", [
    (anthropic.RateLimitError("secret upstream detail", response=api_error(429), body=None), 503),
    (anthropic.InternalServerError("secret upstream detail", response=api_error(500), body=None), 503),
    (anthropic.AuthenticationError("secret upstream detail", response=api_error(401), body=None), 502),
])
def test_model_errors_map_to_status_without_leaking_details(db_path, error, status):

    def fail(**_):
        raise error

    fake = NS(beta=NS(messages=NS(create=fail)))
    app = create_app(Settings(database_path=str(db_path), as_of="2026-11-02"), fake, UsageLimiter(100, 100))
    response = TestClient(app, raise_server_exceptions=False).post("/ask", json={"question": "a"})
    assert response.status_code == status and "secret" not in response.text


def test_unexpected_errors_do_not_leak_and_free_the_concurrency_slot(db_path, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_QUESTIONS", "1")
    calls = []

    def create(**_):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("secret internal detail")
        return text("fine")

    app = create_app(Settings(database_path=str(db_path), as_of="2026-11-02"),
                     NS(beta=NS(messages=NS(create=create))), UsageLimiter(100, 100))
    client = TestClient(app, raise_server_exceptions=False)
    first = client.post("/ask", json={"question": "a"})
    assert first.status_code == 500 and "secret" not in first.text
    assert client.post("/ask", json={"question": "b"}).status_code == 200


def test_page_and_assets_are_served_with_a_strict_csp(db_path):
    client = client_for(db_path)
    page = client.get("/")
    assert page.status_code == 200 and "PaisaLens" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "<script>" not in page.text and "style=" not in page.text  # the CSP would block these


def test_page_never_renders_answers_as_markup():
    from app.main import UI_DIR
    source = (UI_DIR / "app.js").read_text("utf-8")
    assert "innerHTML" not in source and "insertAdjacentHTML" not in source


def test_interactive_docs_are_not_exposed(db_path):
    client = client_for(db_path)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_limiter_window_slides_and_day_resets():
    now = [0.0]
    day = [date(2026, 11, 2)]
    limiter = UsageLimiter(2, 3, clock=lambda: now[0], today=lambda: day[0])
    assert limiter.check("a") is None and limiter.check("a") is None
    assert limiter.check("a") == "client"
    assert limiter.check("b") is None  # another client has its own window, and this uses the last daily slot
    now[0] = 61
    assert limiter.check("a") == "daily"
    day[0] = date(2026, 11, 3)
    assert limiter.check("a") is None


def test_limiter_refusals_do_not_use_up_the_daily_budget():
    limiter = UsageLimiter(1, 2, clock=lambda: 0.0, today=lambda: date(2026, 11, 2))
    assert limiter.check("a") is None
    for _ in range(5):
        assert limiter.check("a") == "client"
    assert limiter.check("b") is None


def test_limiter_forgets_idle_clients():
    now = [0.0]
    limiter = UsageLimiter(5, 1000, clock=lambda: now[0], today=lambda: date(2026, 11, 2))
    for index in range(50):
        limiter.check(f"client-{index}")
    now[0] = 61
    limiter.check("new")
    assert list(limiter._recent) == ["new"]
