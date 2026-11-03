"""HTTP service: the web page, questions, stats, and (when enabled) uploads of your own statements.

Run: uvicorn app.main:app
"""
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import anthropic
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.gemini import GeminiError
from app.agent.loop import Settings, ask, is_gemini_model
from app.agent.tools import present_result
from app.analyses import EXAMPLE_ID, MAX_NAME_LENGTH, AnalysisNotFound, AnalysisStore
from app.ingest.parsers import parse_upload
from app.ingest.statements import StatementError, load, relink_internal_transfers
from app.limits import UsageLimiter
from app.sessions import COOKIE_NAME, SessionManager, SessionsFull
from app.sql.guardrails import GuardrailError, run_query
from app.stats import build_stats, list_transactions

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
MAX_QUESTION_LENGTH = 500
MAX_LOCAL_UPLOAD_MB = 10
MAX_PRIVATE_UPLOAD_MB = 5
MAX_ANALYSES_PER_VISITOR = 3
HSTS_VALUE = "max-age=31536000"
MAX_PAGE_SIZE = 200
MAX_HISTORY_TURNS = 6
MAX_HISTORY_ANSWER_LENGTH = 2000
API_TIMEOUT_SECONDS = 60
API_MAX_RETRIES = 1
ALLOWED_CATEGORIES = ("Bills", "Cash", "Fees", "Food Delivery", "Fuel", "Groceries", "Health", "Other", "People",
                      "Rent", "Shopping", "Subscriptions", "Transport", "Travel")
MONTH_PATTERN = r"^\d{4}-\d{2}$"
WRITE_HEADER_VALUE = "paisalens"

# The page renders answers as plain text and loads nothing from elsewhere, so text injected
# through a statement has no image or link to carry data out with.
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",  # browsers revalidate, so an update never leaves a stale page or script
}

logger = logging.getLogger("paisalens")


class Turn(BaseModel):
    question: str = Field(max_length=MAX_QUESTION_LENGTH)
    answer: str = Field(max_length=MAX_HISTORY_ANSWER_LENGTH)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    analysis: str = EXAMPLE_ID
    history: list[Turn] = Field(default=[], max_length=MAX_HISTORY_TURNS)


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
    table: dict | None = None


class AnalysisChange(BaseModel):
    name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    ai_allowed: bool | None = None


class CategoryChange(BaseModel):
    analysis: str
    merchant: str = Field(min_length=1, max_length=100)
    category: str


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def create_app(settings: Settings | None = None, client=None, limiter: UsageLimiter | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    limiter = limiter or UsageLimiter(int(os.getenv("RATE_LIMIT_PER_MINUTE", "5")),
                                      int(os.getenv("DAILY_QUESTION_LIMIT", "200")))
    in_flight = threading.BoundedSemaphore(int(os.getenv("MAX_CONCURRENT_QUESTIONS", "4")))
    uses_gemini = is_gemini_model(settings.model)
    credentials = ("GEMINI_API_KEY",) if uses_gemini else ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    model_configured = client is not None or any(os.getenv(name) for name in credentials)
    if model_configured and client is None and not uses_gemini:
        client = anthropic.Anthropic(timeout=API_TIMEOUT_SECONDS, max_retries=API_MAX_RETRIES)
    if not model_configured:
        logger.warning("No credentials set for %s: /ask will return 503", settings.model)

    # Two ways to allow uploads: one person on their own machine, or many visitors with temporary private space.
    local_uploads = env_flag("ENABLE_UPLOADS")
    private_uploads = env_flag("PRIVATE_UPLOADS")
    if local_uploads and private_uploads:
        raise ValueError("Set ENABLE_UPLOADS or PRIVATE_UPLOADS, not both.")
    uploads_enabled = local_uploads or private_uploads
    local_store = AnalysisStore(os.getenv("ANALYSES_DIR", "data/analyses")) if local_uploads else None
    retention_minutes = int(os.getenv("SESSION_TTL_MINUTES", "120"))
    sessions = None
    upload_limiter = None
    if private_uploads:
        sessions = SessionManager(os.getenv("SESSIONS_DIR", "/tmp/paisalens-sessions"), retention_minutes * 60,
                                  int(os.getenv("MAX_VISITORS", "100")))
        sessions.start_sweeping()
        upload_limiter = UsageLimiter(int(os.getenv("UPLOADS_PER_MINUTE", "3")), int(os.getenv("DAILY_UPLOAD_LIMIT", "100")))
    max_upload_mb = MAX_PRIVATE_UPLOAD_MB if private_uploads else MAX_LOCAL_UPLOAD_MB
    parsing_slots = threading.BoundedSemaphore(1 if private_uploads else 4)  # reading PDFs is heavy on a small server
    write_lock = threading.Lock()

    app = FastAPI(title="PaisaLens", docs_url=None, redoc_url=None, openapi_url=None)
    if local_uploads:
        # Stops another website reaching this local service through DNS rebinding.
        hosts = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if not request.url.path.startswith("/static") and request.url.path != "/":
            response.headers["Cache-Control"] = "no-store"  # financial data shouldn't linger in browser caches
        if private_uploads:
            response.headers["Strict-Transport-Security"] = HSTS_VALUE
        return response

    def require_uploads():
        if not uploads_enabled:
            raise HTTPException(404, "Uploads aren't available on this deployment.")

    def require_write_header(x_requested_with: str = Header(default="")):
        # Browsers won't send a custom header cross-site without a CORS approval this app never gives,
        # so other websites can't make your browser change your data.
        if x_requested_with != WRITE_HEADER_VALUE:
            raise HTTPException(403, "Missing request header.")

    def store_for(request: Request) -> AnalysisStore | None:
        """This visitor's analyses. Everything below goes through here, which is what keeps visitors apart."""
        if local_uploads:
            return local_store
        return sessions.get(request.cookies.get(COOKIE_NAME)) if private_uploads else None

    def owned_store(request: Request) -> AnalysisStore:
        store = store_for(request)
        if store is None:
            raise HTTPException(404, "That analysis has expired or doesn't exist.")
        return store

    def database_for(analysis: str, request: Request) -> Path | str:
        if analysis == EXAMPLE_ID:
            return settings.database_path
        require_uploads()
        try:
            return owned_store(request).existing_path(analysis)
        except AnalysisNotFound as exc:
            raise HTTPException(404, "That analysis has expired or doesn't exist.") from exc

    def read_statement(request: Request, file: UploadFile, account: str, password: str, self_name: str):
        # The file itself is never saved: only the parsed transactions are kept.
        if upload_limiter is not None:
            refused = upload_limiter.check(request.client.host if request.client else "unknown")
            if refused == "daily":
                raise HTTPException(429, "The site has reached its daily upload limit. Try again tomorrow.")
            if refused:
                raise HTTPException(429, "Too many uploads. Please wait a minute.", headers={"Retry-After": "60"})
        data = file.file.read(max_upload_mb * 1024 * 1024 + 1)
        if len(data) > max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"That file is over the {max_upload_mb} MB limit.")
        label = re.sub(r"[^A-Za-z0-9 _-]", "", account).strip() or "My account"
        names = frozenset({self_name.strip()}) if self_name.strip() else frozenset()
        if not parsing_slots.acquire(blocking=False):
            raise HTTPException(503, "The service is busy reading another statement. Please try again shortly.")
        try:
            return parse_upload(file.filename or "", data, label, password, names)
        except StatementError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            parsing_slots.release()

    def import_statement(conn, parsed) -> dict:
        added = load(conn, parsed.transactions)
        relink_internal_transfers(conn)
        logger.info(json.dumps({"event": "upload", "rows": len(parsed.transactions), "added": added}))
        return {"read": len(parsed.transactions), "added": added, "duplicates": len(parsed.transactions) - added,
                "skipped_rows": parsed.skipped_rows, "balance_checked": parsed.balance_checked,
                "balance_mismatches": parsed.balance_mismatches}

    @app.get("/health")
    def health():
        # Touches the database but never the model, so frequent probes cost nothing.
        try:
            count = run_query(settings.database_path, "SELECT COUNT(*) FROM all_transactions").rows[0][0]
        except (GuardrailError, sqlite3.Error) as exc:
            logger.error("Health check failed: %s", exc)
            raise HTTPException(503, "Database unavailable") from exc
        return {"status": "ok", "transactions": count, "model_configured": model_configured}

    @app.get("/config")
    def config():
        return {"uploads_enabled": uploads_enabled, "storage": "temporary" if private_uploads else "local",
                "retention_minutes": retention_minutes, "max_upload_mb": max_upload_mb, "categories": ALLOWED_CATEGORIES}

    @app.get("/analyses")
    def analyses(request: Request):
        store = store_for(request)
        return {"analyses": store.list() if store is not None else []}

    @app.post("/ask", response_model=AskResponse)
    def ask_question(body: AskRequest, request: Request):
        if not model_configured:
            raise HTTPException(503, "The service has no model credentials configured.")
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Question is empty.")
        database = database_for(body.analysis, request)
        if body.analysis != EXAMPLE_ID and not owned_store(request).get(body.analysis)["ai_allowed"]:
            raise HTTPException(403, "Questions are off for this analysis, because they would send its data to the "
                                     "AI provider. You can turn them on under Manage.")
        run_settings = settings if body.analysis == EXAMPLE_ID else replace(settings, database_path=str(database), as_of="")

        refused = limiter.check(request.client.host if request.client else "unknown")
        if refused == "daily":
            raise HTTPException(429, "The demo has reached its daily question limit. Try again tomorrow.")
        if refused:
            raise HTTPException(429, "Too many questions. Please wait a minute.", headers={"Retry-After": "60"})
        if not in_flight.acquire(blocking=False):
            raise HTTPException(503, "The service is busy. Please try again shortly.")

        started = time.perf_counter()
        try:
            history = [(turn.question, turn.answer) for turn in body.history]
            answer = ask(question, run_settings, client, history)
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
            logger.warning("Model temporarily unavailable: %s", type(exc).__name__)
            raise HTTPException(503, "The model is temporarily unavailable. Please try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            logger.error("Model API error %s, request id %s", exc.status_code, exc.request_id)
            raise HTTPException(502, "The model request failed.") from exc
        except GeminiError as exc:
            logger.warning("Gemini API error %s", exc.status)
            if exc.temporary:
                raise HTTPException(503, "The model is temporarily unavailable. Please try again shortly.") from exc
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
                                       cache_write_tokens=answer.cache_write_tokens),
                           table=present_result(answer.results[-1] if answer.results else None))

    @app.get("/stats")
    def stats(request: Request, analysis: str = EXAMPLE_ID, month: str | None = Query(default=None, pattern=MONTH_PATTERN)):
        return build_stats(database_for(analysis, request), month)

    @app.get("/transactions")
    def transactions(request: Request, analysis: str = EXAMPLE_ID, search: str = Query(default="", max_length=100),
                     category: str = Query(default="", max_length=50),
                     month: str = Query(default="", pattern=r"^(\d{4}-\d{2})?$"),
                     limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE), offset: int = Query(default=0, ge=0)):
        return list_transactions(database_for(analysis, request), search, category, month, limit, offset)

    @app.post("/analyses", dependencies=[Depends(require_uploads), Depends(require_write_header)])
    def create_analysis(request: Request, response: Response, file: UploadFile = File(...),
                        name: str = Form(default="", max_length=MAX_NAME_LENGTH),
                        account: str = Form(default="My account", max_length=40),
                        password: str = Form(default="", max_length=100), self_name: str = Form(default="", max_length=60)):
        store = store_for(request)
        if private_uploads and store is not None and len(store.list()) >= MAX_ANALYSES_PER_VISITOR:
            raise HTTPException(409, f"You can keep {MAX_ANALYSES_PER_VISITOR} analyses at a time. Delete one to add another.")
        # The statement is read first, so a bad file doesn't leave an empty analysis or visitor space behind.
        parsed = read_statement(request, file, account, password, self_name)
        fallback = Path(file.filename or "").stem.replace("_", " ").replace("-", " ")
        with write_lock:
            if store is None:
                try:
                    token, store = sessions.create()
                except SessionsFull as exc:
                    raise HTTPException(503, "Too many people are using this right now. Please try again later.") from exc
                # Not readable by scripts, sent only over HTTPS, and never sent from other sites.
                response.set_cookie(COOKIE_NAME, token, httponly=True, secure=True, samesite="strict", path="/")
            analysis_id, conn = store.create(name.strip() or fallback)
            try:
                report = import_statement(conn, parsed)
            finally:
                conn.close()
        return {"analysis": store.get(analysis_id), "report": report}

    @app.post("/analyses/{analysis_id}/statements", dependencies=[Depends(require_uploads), Depends(require_write_header)])
    def add_statement(request: Request, analysis_id: str, file: UploadFile = File(...), account: str = Form(default="My account", max_length=40),
                      password: str = Form(default="", max_length=100), self_name: str = Form(default="", max_length=60)):
        database_for(analysis_id, request)
        if analysis_id == EXAMPLE_ID:
            raise HTTPException(403, "The example analysis can't be changed.")
        store = owned_store(request)
        parsed = read_statement(request, file, account, password, self_name)
        with write_lock:
            conn = store.open_writable(analysis_id)
            try:
                report = import_statement(conn, parsed)
            finally:
                conn.close()
        return {"analysis": store.get(analysis_id), "report": report}

    @app.patch("/analyses/{analysis_id}", dependencies=[Depends(require_uploads), Depends(require_write_header)])
    def change_analysis(request: Request, analysis_id: str, body: AnalysisChange):
        database_for(analysis_id, request)
        if analysis_id == EXAMPLE_ID:
            raise HTTPException(403, "The example analysis can't be changed.")
        store = owned_store(request)
        with write_lock:
            store.update(analysis_id, body.name, body.ai_allowed)
        return store.get(analysis_id)

    @app.delete("/analyses/{analysis_id}", dependencies=[Depends(require_uploads), Depends(require_write_header)])
    def delete_analysis(request: Request, analysis_id: str):
        database_for(analysis_id, request)
        if analysis_id == EXAMPLE_ID:
            raise HTTPException(403, "The example analysis can't be deleted.")
        store = owned_store(request)
        with write_lock:
            store.delete(analysis_id)
        return {"deleted": True}

    @app.patch("/categories", dependencies=[Depends(require_uploads), Depends(require_write_header)])
    def change_category(request: Request, body: CategoryChange):
        if body.category not in ALLOWED_CATEGORIES:
            raise HTTPException(422, "Unknown category.")
        database_for(body.analysis, request)
        if body.analysis == EXAMPLE_ID:
            raise HTTPException(403, "The example analysis can't be changed.")
        store = owned_store(request)
        with write_lock:
            conn = store.open_writable(body.analysis)
            try:
                conn.execute("INSERT OR REPLACE INTO category_overrides (merchant, category) VALUES (?, ?)",
                             (body.merchant, body.category))
                changed = conn.execute("""UPDATE transactions SET category = ? WHERE merchant = ?
                                          AND txn_type NOT IN ('internal_transfer', 'card_payment', 'investment', 'income')""",
                                       (body.category, body.merchant)).rowcount
                conn.commit()
            finally:
                conn.close()
        return {"changed": changed}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(UI_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")
    return app


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
app = create_app()
