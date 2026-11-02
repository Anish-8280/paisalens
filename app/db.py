"""SQLite connections: one writable path for ingestion, one locked-down path for the agent."""
import sqlite3
from importlib import resources
from pathlib import Path


def connect_writable(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(resources.files("app.sql").joinpath("schema.sql").read_text("utf-8"))
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn
