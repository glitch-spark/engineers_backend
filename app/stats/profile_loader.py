"""DB reads for the Statistics "Your profiles" report and the coverage line (the `applications` table)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Optional

from app.applications.rows import interview_outcome
from app.models import Application, Interview

_ROW_FIELDS = ("key", "accountId", "ownerId", "appliedAt", "by", "bidStatus", "interviewId", "daysToInterview",
               "furthest", "firstRoundAt", "board", "scoreBand", "score", "area", "profileAgeDays", "bidderId",
               "templateName")
_TTL = 300  # seconds
_MAX = 16
# cached lists are shared between callers: read-only. Entries hold the load's task, so concurrent misses for one
# key (cards mount together) await a single query; a failed load is dropped, not cached.
_cache: dict[tuple, tuple[float, asyncio.Task]] = {}


def clear_cache() -> None:
    _cache.clear()


async def _fetch(start: datetime, end: datetime, region: str) -> list[dict]:
    # byInterviewOnly rows (interview-proven applications) count like any other row
    q: dict = {"appliedAt": {"$gte": start, "$lt": end}}
    if region != "all":
        q["region"] = region
    return await Application.get_motor_collection().find(q, {f: 1 for f in _ROW_FIELDS}).to_list(None)


async def load_period_rows(start: datetime, end: datetime, region: str) -> list[dict]:
    ck = (start, end, region)
    now = time.monotonic()
    entry = _cache.get(ck)
    if not entry or now - entry[0] >= _TTL:
        for k in [k for k, (t, _) in _cache.items() if now - t >= _TTL]:
            del _cache[k]
        while len(_cache) >= _MAX:
            del _cache[min(_cache, key=lambda k: _cache[k][0])]
        entry = _cache[ck] = (now, asyncio.ensure_future(_fetch(start, end, region)))
    try:
        return await asyncio.shield(entry[1])  # one caller cancelled (client gone) must not cancel the others
    except Exception:
        if _cache.get(ck) is entry:
            del _cache[ck]
        raise


async def count_unmatched_interviews(since: datetime, until: Optional[datetime] = None, owner_id=None,
                                     region: str = "all", regions: Optional[dict] = None) -> int:
    """Interviews created in [since, until) (optionally by one owner, and on a profile of `region` per the
    accountId -> region map `regions`) that count and that no row of the table carries."""
    created: dict = {"$gte": since}
    if until:
        created["$lt"] = until
    q: dict = {"createdAt": created}
    if owner_id is not None:
        q["createdBy"] = owner_id
    carried = set(await Application.get_motor_collection().distinct("interviewId"))
    ivs = await Interview.get_motor_collection().find(
        q, {f: 1 for f in ("accountId", "createdBy", "scheduledAt", "stage", "status", "stageHistory", "createdAt", "updatedAt")},
    ).to_list(None)
    if region != "all":
        ivs = [d for d in ivs if (regions or {}).get(d.get("accountId")) == region]
    return sum(1 for d in ivs if d["_id"] not in carried and interview_outcome(Interview.model_validate(d)))


async def count_untied_interviews(account_ids: list, start: datetime, end: datetime) -> int:
    """Interviews on `account_ids` whose first countable round is in [start, end) and that no row carries."""
    if not account_ids:
        return 0
    carried = set(await Application.get_motor_collection().distinct("interviewId"))
    # ponytail: scans every interview of the scope's profiles; add a round-date prefilter if it gets slow
    ivs = await Interview.get_motor_collection().find({"accountId": {"$in": list(account_ids)}}).to_list(None)
    n = 0
    for d in ivs:
        if d["_id"] in carried:
            continue
        out = interview_outcome(Interview.model_validate(d))
        if out and out["firstRoundAt"] and start <= out["firstRoundAt"] < end:
            n += 1
    return n


async def accounts_with_rounds_since(account_ids: list, since: datetime) -> set[str]:
    """Profiles (of `account_ids`) with any interview round scheduled at or after `since`, matched to a row or not."""
    if not account_ids:
        return set()
    q = {"accountId": {"$in": list(account_ids)},
         "$or": [{"stageHistory.scheduledAt": {"$gte": since}}, {"scheduledAt": {"$gte": since}}]}
    return {str(a) for a in await Interview.get_motor_collection().distinct("accountId", q)}
