"""Private, temporary space for each visitor to a public deployment. There are no accounts.

A visitor holds a random token in a cookie, and their analyses live in a folder named after the
token's hash, so a stolen copy of the server's folder names can't be used as cookies. Every lookup
goes through that folder: another visitor's analysis ID simply doesn't exist from where you stand.
Everything is deleted after a period of inactivity, and whenever the server starts.
"""
import hashlib
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from app.analyses import AnalysisStore

COOKIE_NAME = "paisalens_session"
FOLDER_PATTERN = re.compile(r"^[a-f0-9]{64}$")
MAX_TOKEN_LENGTH = 100
SWEEP_INTERVAL_SECONDS = 300


class SessionsFull(Exception):
    pass


class SessionManager:
    def __init__(self, root: str | Path, ttl_seconds: int, max_sessions: int = 100, clock=time.time):
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._clock = clock
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for folder in self._folders():
            shutil.rmtree(folder, ignore_errors=True)

    def _folders(self) -> list[Path]:
        return [path for path in self.root.iterdir() if path.is_dir() and FOLDER_PATTERN.match(path.name)]

    def _folder(self, token: str) -> Path:
        return self.root / hashlib.sha256(token.encode()).hexdigest()

    def _expired(self, folder: Path) -> bool:
        return self._clock() - folder.stat().st_mtime > self.ttl_seconds

    def _touch(self, folder: Path) -> None:
        now = self._clock()
        os.utime(folder, (now, now))

    def create(self) -> tuple[str, AnalysisStore]:
        with self._lock:
            self._sweep_locked()
            if len(self._folders()) >= self.max_sessions:
                raise SessionsFull
            token = secrets.token_urlsafe(32)
            folder = self._folder(token)
            folder.mkdir(mode=0o700)
            self._touch(folder)
            return token, AnalysisStore(folder / "analyses")

    def get(self, token: str | None) -> AnalysisStore | None:
        """The visitor's store, or None. Only tokens this server issued lead anywhere."""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        folder = self._folder(token)
        with self._lock:
            if not folder.is_dir():
                return None
            if self._expired(folder):
                shutil.rmtree(folder, ignore_errors=True)
                return None
            self._touch(folder)
        return AnalysisStore(folder / "analyses")

    def _sweep_locked(self) -> None:
        for folder in self._folders():
            if self._expired(folder):
                shutil.rmtree(folder, ignore_errors=True)

    def sweep(self) -> None:
        with self._lock:
            self._sweep_locked()

    def start_sweeping(self) -> None:
        """Delete expired data on a timer, so the promise holds even if nobody visits again."""
        def run():
            while True:
                time.sleep(SWEEP_INTERVAL_SECONDS)
                self.sweep()

        threading.Thread(target=run, name="session-sweeper", daemon=True).start()
