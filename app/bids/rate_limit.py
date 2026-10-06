"""In-process brute-force limit for /ext/login and /ext/register (per process; one API process)."""

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional


USERNAME_KEY_LEN = 100
IP_KEY_LEN = 64


class FailureLimiter:
    def __init__(self, limit: int = 10, window: timedelta = timedelta(minutes=15), max_keys: int = 10_000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys  # past this many keys, reserve() first drops the stale ones
        self._failures: dict[str, list[datetime]] = defaultdict(list)

    def _sweep(self, now: datetime) -> None:
        """Forget keys whose newest attempt is outside the window (many one-off usernames or IPs)."""
        stale = [k for k, attempts in self._failures.items() if not attempts or now - max(attempts) >= self.window]
        for key in stale:
            del self._failures[key]

    def _live(self, key: str, now: datetime) -> list[datetime]:
        live = [t for t in self._failures.get(key, ()) if now - t < self.window]
        if live:
            self._failures[key] = live
        else:
            self._failures.pop(key, None)
        return live

    def reserve(self, keys: list[str], now: datetime) -> Optional[int]:
        """Atomically (no await inside) count an attempt on every key, unless one is at the limit.

        Returns the retry-after seconds when blocked (nothing recorded), else None. The reservation IS the
        recorded failure; call release() when the attempt turns out to be a success.
        """
        if len(self._failures) > self.max_keys:
            self._sweep(now)
        waits = []
        for key in keys:
            live = self._live(key, now)
            if len(live) >= self.limit:
                waits.append((live[0] + self.window - now).total_seconds())
        if waits:
            return max(1, math.ceil(max(waits)))
        for key in keys:
            self._failures[key].append(now)
        return None

    def release(self, keys: list[str], now: datetime) -> None:
        """Undo the reservation made with this exact `now`."""
        for key in keys:
            attempts = self._failures.get(key)
            if attempts and now in attempts:
                attempts.remove(now)
                if not attempts:
                    self._failures.pop(key, None)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


LIMITER = FailureLimiter()


def client_ip(request) -> str:
    """First X-Forwarded-For entry (Railway's proxy), else the socket peer. Capped: the header is client-sent."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()[:IP_KEY_LEN]
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def limiter_keys(username: str, ip: str) -> list[str]:
    """[username key, IP key], each capped so a long input can't make a big key."""
    return [f"user:{username[:USERNAME_KEY_LEN]}", f"ip:{ip[:IP_KEY_LEN]}"]
