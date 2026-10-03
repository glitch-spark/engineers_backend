"""In-process brute-force limit for /ext/login and /ext/register (per process; one API process)."""

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional


class FailureLimiter:
    def __init__(self, limit: int = 10, window: timedelta = timedelta(minutes=15)):
        self.limit = limit
        self.window = window
        self._failures: dict[str, list[datetime]] = defaultdict(list)

    def _live(self, key: str, now: datetime) -> list[datetime]:
        live = [t for t in self._failures.get(key, ()) if now - t < self.window]
        if live:
            self._failures[key] = live
        else:
            self._failures.pop(key, None)
        return live

    def retry_after(self, keys: list[str], now: datetime) -> Optional[int]:
        """Seconds until the most-blocked key frees a slot; None when no key is at the limit."""
        waits = []
        for key in keys:
            live = self._live(key, now)
            if len(live) >= self.limit:
                waits.append((live[0] + self.window - now).total_seconds())
        return max(1, math.ceil(max(waits))) if waits else None

    def fail(self, keys: list[str], now: datetime) -> None:
        for key in keys:
            self._live(key, now)
            self._failures[key].append(now)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


LIMITER = FailureLimiter()


def client_ip(request) -> str:
    """First X-Forwarded-For entry (Railway's proxy), else the socket peer."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"
