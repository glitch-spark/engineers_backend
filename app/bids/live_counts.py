"""Live (not yet reported) bid counts for the open daily/weekly windows, cached briefly."""

from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from app.bids.records import count_by_status, uses_records
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
    # Each open window is judged by its own start: after BID_RECORDS_START it counts bid records, else folders.
    by_records = [uses_records(start, sched) for start, _ in windows]
    folder_ix = [i for i, r in enumerate(by_records) if not r]
    folder_windows = [windows[i] for i in folder_ix]

    # bidder id -> (count, error) per window, in _KINDS order
    out_counts: dict[str, list[tuple[Optional[int], Optional[str]]]] = {str(b.id): [(None, None)] * len(windows) for b in bidders}

    if len(folder_ix) < len(windows):
        record_ix = [i for i in range(len(windows)) if by_records[i]]
        for i in record_ix:
            start, end = windows[i]
            by_status = await count_by_status([b.id for b in bidders], start, end)
            for b in bidders:
                out_counts[str(b.id)][i] = (by_status[str(b.id)][0], None)

    if folder_ix:
        # bidder id -> one cache key per folder window
        keys = {
            str(b.id): [(str(b.id), b.screenshotFolderUrl, _KINDS[i][1], windows[i][0].isoformat()) for i in folder_ix]
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

        # A miss on any folder window re-counts all of them: one folder listing serves every window.
        for bid, (counts, error) in (await count_folder_windows(misses, folder_windows, storage)).items():
            for j, key in enumerate(keys[bid]):
                resolved[key] = (None if counts is None else counts[j], error)
                cache.set(key, resolved[key], now)

        for b in bidders:
            for j, i in enumerate(folder_ix):
                out_counts[str(b.id)][i] = resolved[keys[str(b.id)][j]]

    out: dict[str, dict] = {}
    for b in bidders:
        today, week = out_counts[str(b.id)]
        out[str(b.id)] = {"today": today[0], "week": week[0], "error": today[1] or week[1]}
    return out
