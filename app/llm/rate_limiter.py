from __future__ import annotations

import time
from collections import deque
from threading import Lock

_lock = Lock()
_timestamps: deque[float] = deque()


def is_exhausted(max_rpm: int) -> bool:
    """Return True when the global NVIDIA free-tier window is full."""
    if max_rpm <= 0:
        return True
    now = time.monotonic()
    window = 60.0
    with _lock:
        while _timestamps and now - _timestamps[0] >= window:
            _timestamps.popleft()
        return len(_timestamps) >= max_rpm


def acquire(max_rpm: int) -> bool:
    """Record one NVIDIA free-tier request if capacity remains."""
    if max_rpm <= 0:
        return False
    now = time.monotonic()
    window = 60.0
    with _lock:
        while _timestamps and now - _timestamps[0] >= window:
            _timestamps.popleft()
        if len(_timestamps) >= max_rpm:
            return False
        _timestamps.append(now)
        return True


def reset_for_tests() -> None:
    with _lock:
        _timestamps.clear()
