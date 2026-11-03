FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/paisalens.db \
    AS_OF_DATE=2026-11-02

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY app ./app
COPY ui ./ui
COPY data/sample ./data/sample

# The image holds synthetic data only, built into a database nothing at runtime can write to.
RUN python -m app.ingest.statements data/sample/transactions.csv --db "$DATABASE_PATH" \
    && chmod 444 "$DATABASE_PATH" \
    && useradd --create-home --uid 10001 paisalens \
    && mkdir /userdata && chown paisalens /userdata

# Only used when uploads are enabled, which needs a volume here. See compose.yaml.
ENV ANALYSES_DIR=/userdata/analyses
ENV SESSIONS_DIR=/tmp/paisalens-sessions

USER paisalens
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8000\")}/health', timeout=4)"

# One worker on purpose, because usage limits live in the process. Hosts that assign a port set $PORT.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
