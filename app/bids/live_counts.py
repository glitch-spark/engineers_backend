"""Live (not yet reported) bid counts for the open daily/weekly windows, cached briefly."""

from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from app.bids.storage import BidStorage, count_folders
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
    # field -> {bidder id -> (count, error)}
    resolved: dict[str, dict[str, tuple[Optional[int], Optional[str]]]] = {}
    for field, kind in _KINDS:
        start, end = open_window(kind, now, sched)
        found: dict[str, tuple[Optional[int], Optional[str]]] = {}
        misses: list[tuple[str, str]] = []
        keys: dict[str, tuple] = {}
        for b in bidders:
            bid = str(b.id)
            key = (bid, b.screenshotFolderUrl, kind, start.isoformat())
            keys[bid] = key
            cached = cache.get(key, now)
            if cached is None:
                misses.append((bid, b.screenshotFolderUrl))
            else:
                found[bid] = cached
        for bid, result in (await count_folders(misses, start, end, storage)).items():
            cache.set(keys[bid], result, now)
            found[bid] = result
        resolved[field] = found

    out: dict[str, dict] = {}
    for b in bidders:
        bid = str(b.id)
        today, week = resolved["today"][bid], resolved["week"][bid]
        out[bid] = {"today": today[0], "week": week[0], "error": today[1] or week[1]}
    return out
