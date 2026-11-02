"""HTTP service: the web page, POST /ask and GET /health.

Run: uvicorn app.main:app
"""
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.loop import Settings, ask
from app.limits import UsageLimiter
from app.sql.guardrails import GuardrailError, run_query

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
MAX_QUESTION_LENGTH = 500
API_TIMEOUT_SECONDS = 60
API_MAX_RETRIES = 1

# The page renders answers as plain text and loads nothing from elsewhere, so text injected
# through a statement has no image or link to carry data out with.
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

logger = logging.getLogger("paisalens")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class AskResponse(BaseModel):
    answer: str
    status: str
    sql: list[str]
    steps: int
    usage: Usage


def create_app(settings: Settings | None = None, client=None, limiter: UsageLimiter | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    limiter = limiter or UsageLimiter(int(os.getenv("RATE_LIMIT_PER_MINUTE", "5")),
                                      int(os.getenv("DAILY_QUESTION_LIMIT", "200")))
    in_flight = threading.BoundedSemaphore(int(os.getenv("MAX_CONCURRENT_QUESTIONS", "4")))
    model_configured = client is not None or bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    if model_configured and client is None:
        client = anthropic.Anthropic(timeout=API_TIMEOUT_SECONDS, max_retries=API_MAX_RETRIES)
    if not model_configured:
        logger.warning("No Anthropic credentials set: /ask will return 503")

    app = FastAPI(title="PaisaLens", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/health")
    def health():
        # Touches the database but never the model, so frequent probes cost nothing.
        try:
            count = run_query(settings.database_path, "SELECT COUNT(*) FROM all_transactions").rows[0][0]
        except (GuardrailError, sqlite3.Error) as exc:
            logger.error("Health check failed: %s", exc)
            raise HTTPException(503, "Database unavailable") from exc
        return {"status": "ok", "transactions": count, "model_configured": model_configured}

    @app.post("/ask", response_model=AskResponse)
    def ask_question(body: AskRequest, request: Request):
        if not model_configured:
            raise HTTPException(503, "The service has no model credentials configured.")
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Question is empty.")

        refused = limiter.check(request.client.host if request.client else "unknown")
        if refused == "daily":
            raise HTTPException(429, "The demo has reached its daily question limit. Try again tomorrow.")
        if refused:
            raise HTTPException(429, "Too many questions. Please wait a minute.", headers={"Retry-After": "60"})
        if not in_flight.acquire(blocking=False):
            raise HTTPException(503, "The service is busy. Please try again shortly.")

        started = time.perf_counter()
        try:
            answer = ask(question, settings, client)
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
            logger.warning("Model temporarily unavailable: %s", type(exc).__name__)
            raise HTTPException(503, "The model is temporarily unavailable. Please try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            logger.error("Model API error %s, request id %s", exc.status_code, exc.request_id)
            raise HTTPException(502, "The model request failed.") from exc
        finally:
            in_flight.release()

        # Metrics only: questions and answers can be about someone's finances, so they stay out of logs.
        logger.info(json.dumps({"event": "ask", "status": answer.status, "steps": answer.steps,
                                "seconds": round(time.perf_counter() - started, 2),
                                "input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens,
                                "cache_read_tokens": answer.cache_read_tokens}))
        return AskResponse(answer=answer.text, status=answer.status, sql=answer.sql, steps=answer.steps,
                           usage=Usage(input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
                                       cache_read_tokens=answer.cache_read_tokens,
                                       cache_write_tokens=answer.cache_write_tokens))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(UI_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")
    return app


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
app = create_app()
