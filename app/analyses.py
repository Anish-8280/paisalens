"""Each analysis is its own SQLite file, so one person's statements can never mix with another set."""
import re
import sqlite3
import time
import uuid
from pathlib import Path

from app.db import connect_readonly, connect_writable

EXAMPLE_ID = "example"
ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
MAX_NAME_LENGTH = 60


class AnalysisNotFound(Exception):
    pass


def clean_analysis_name(name: str, fallback: str = "New analysis") -> str:
    cleaned = " ".join(re.sub(r"[\x00-\x1f]", " ", name or "").split())[:MAX_NAME_LENGTH]
    return cleaned or fallback


class AnalysisStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._adopt_legacy_database()

    def _adopt_legacy_database(self) -> None:
        """Statements uploaded before analyses existed become one analysis, rather than disappearing."""
        legacy = self.directory.parent / "my.db"
        if legacy.exists():
            analysis_id = uuid.uuid4().hex
            self.directory.mkdir(parents=True, exist_ok=True)
            legacy.replace(self.path(analysis_id))
            conn = connect_writable(self.path(analysis_id))
            self._write_meta(conn, name="My analysis", created_at=str(time.time()), ai_allowed="0")
            conn.close()

    def path(self, analysis_id: str) -> Path:
        # The ID comes from the request and becomes a filename, so its format is checked first.
        if not ID_PATTERN.match(analysis_id or ""):
            raise AnalysisNotFound(analysis_id)
        return self.directory / f"{analysis_id}.db"

    def existing_path(self, analysis_id: str) -> Path:
        path = self.path(analysis_id)
        if not path.exists():
            raise AnalysisNotFound(analysis_id)
        return path

    @staticmethod
    def _write_meta(conn: sqlite3.Connection, **values: str) -> None:
        conn.executemany("INSERT OR REPLACE INTO analysis_meta (key, value) VALUES (?, ?)", list(values.items()))
        conn.commit()

    def create(self, name: str) -> tuple[str, sqlite3.Connection]:
        self.directory.mkdir(parents=True, exist_ok=True)
        analysis_id = uuid.uuid4().hex
        conn = connect_writable(self.path(analysis_id))
        self._write_meta(conn, name=clean_analysis_name(name), created_at=str(time.time()), ai_allowed="0")
        return analysis_id, conn

    def open_writable(self, analysis_id: str) -> sqlite3.Connection:
        return connect_writable(self.existing_path(analysis_id))

    def get(self, analysis_id: str) -> dict:
        conn = connect_readonly(self.existing_path(analysis_id))
        try:
            meta = dict(conn.execute("SELECT key, value FROM analysis_meta").fetchall())
            count, first_date, last_date = conn.execute(
                "SELECT COUNT(*), MIN(txn_date), MAX(txn_date) FROM all_transactions").fetchone()
            accounts = [row[0] for row in conn.execute("SELECT DISTINCT account FROM all_transactions ORDER BY 1")]
        finally:
            conn.close()
        return {"id": analysis_id, "name": meta.get("name", "Analysis"), "example": False,
                "ai_allowed": meta.get("ai_allowed") == "1", "created_at": float(meta.get("created_at", 0)),
                "transactions": count, "first_date": first_date, "last_date": last_date, "accounts": accounts}

    def list(self) -> list[dict]:
        if not self.directory.exists():
            return []
        found = [self.get(path.stem) for path in self.directory.glob("*.db") if ID_PATTERN.match(path.stem)]
        return sorted(found, key=lambda analysis: -analysis["created_at"])

    def update(self, analysis_id: str, name: str | None = None, ai_allowed: bool | None = None) -> None:
        conn = self.open_writable(analysis_id)
        try:
            if name is not None:
                self._write_meta(conn, name=clean_analysis_name(name, "Analysis"))
            if ai_allowed is not None:
                self._write_meta(conn, ai_allowed="1" if ai_allowed else "0")
        finally:
            conn.close()

    def delete(self, analysis_id: str) -> None:
        path = self.existing_path(analysis_id)
        path.unlink()
        path.with_name(path.name + "-journal").unlink(missing_ok=True)
