"""Live (not yet reported) bid counts for the open daily/weekly windows, cached briefly."""

from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from app.bids.storage import BidStorage, count_folder_windows
from app.bids.windows import BidSchedule, open_window

_KINDS = (("today", "daily"), ("week", "weekly"))


class TtlCache:
    def __init__(self, ttl: timedelta):
        self._ttl = ttl
        self._items: dict[Any, tuple[datetime, Any]] = {}

    def get(self, key, now: datetime) -> Optional[Any]:
        hit = self._items.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if now - stored_at >= self._ttl:
            del self._items[key]
            return None
        return value

    def set(self, key, value, now: datetime) -> None:
        self._items[key] = (now, value)


CACHE = TtlCache(timedelta(minutes=5))


async def live_counts(
    bidders: Sequence, now: datetime, sched: BidSchedule, storage: BidStorage, cache: TtlCache
) -> dict[str, dict]:
    windows = [open_window(kind, now, sched) for _, kind in _KINDS]
    # bidder id -> one cache key per window, in _KINDS order
    keys = {
        str(b.id): [
            (str(b.id), b.screenshotFolderUrl, kind, start.isoformat())
            for (_, kind), (start, _) in zip(_KINDS, windows)
        ]
        for b in bidders
    }
    # cache key -> (count, error)
    resolved: dict[tuple, tuple[Optional[int], Optional[str]]] = {}
    misses: list[tuple[str, str]] = []
    for b in bidders:
        hits = [cache.get(key, now) for key in keys[str(b.id)]]
        if any(hit is None for hit in hits):
            misses.append((str(b.id), b.screenshotFolderUrl))
        else:
            resolved.update(zip(keys[str(b.id)], hits))

    # A miss on either window re-counts both: one folder listing serves every window.
    for bid, (counts, error) in (await count_folder_windows(misses, windows, storage)).items():
        for i, key in enumerate(keys[bid]):
            resolved[key] = (None if counts is None else counts[i], error)
            cache.set(key, resolved[key], now)

    out: dict[str, dict] = {}
    for b in bidders:
        today, week = (resolved[key] for key in keys[str(b.id)])
        out[str(b.id)] = {"today": today[0], "week": week[0], "error": today[1] or week[1]}
    return out
