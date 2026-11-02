"""In-memory usage limits. A public demo spends the deployer's API key on every question.

State lives in the process, so run a single worker. A restart resets the counts, which is why
the README also tells deployers to set a spend limit on the API key itself.
"""
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

WINDOW_SECONDS = 60


class UsageLimiter:
    def __init__(self, per_minute: int, per_day: int, clock=time.monotonic, today=None):
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._today = today or (lambda: datetime.now(timezone.utc).date())
        self._lock = threading.Lock()
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        self._day = None
        self._day_count = 0

    def check(self, client_id: str) -> str | None:
        """Record one question and return None, or return why it was refused without recording it."""
        with self._lock:
            today = self._today()
            if today != self._day:
                self._day, self._day_count = today, 0
            if self._day_count >= self.per_day:
                return "daily"

            now = self._clock()
            for key in list(self._recent):  # dropping idle clients keeps this bounded on a public service
                times = self._recent[key]
                while times and now - times[0] >= WINDOW_SECONDS:
                    times.popleft()
                if not times:
                    del self._recent[key]

            times = self._recent[client_id]
            if len(times) >= self.per_minute:
                return "client"
            times.append(now)
            self._day_count += 1
            return None
