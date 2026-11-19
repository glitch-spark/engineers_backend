"""DB reads for the Statistics "Your profiles" report and the coverage line (the `applications` table)."""
from __future__ import annotations

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
_cache: dict[tuple, tuple[float, list[dict]]] = {}  # cached lists are shared between callers: read-only


def clear_cache() -> None:
    _cache.clear()


async def load_period_rows(start: datetime, end: datetime, region: str) -> list[dict]:
    ck = (start, end, region)
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    q: dict = {"appliedAt": {"$gte": start, "$lt": end}}
    if region != "all":
        q["region"] = region
    rows = await Application.get_motor_collection().find(q, {f: 1 for f in _ROW_FIELDS}).to_list(None)
    now = time.monotonic()
    for k in [k for k, (t, _) in _cache.items() if now - t >= _TTL]:
        del _cache[k]
    while len(_cache) >= _MAX:
        del _cache[min(_cache, key=lambda k: _cache[k][0])]
    _cache[ck] = (now, rows)
    return rows


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


async def accounts_with_rounds_since(account_ids: list, since: datetime) -> set[str]:
    """Profiles (of `account_ids`) with any interview round scheduled at or after `since`, matched to a row or not."""
    if not account_ids:
        return set()
    q = {"accountId": {"$in": list(account_ids)},
         "$or": [{"stageHistory.scheduledAt": {"$gte": since}}, {"scheduledAt": {"$gte": since}}]}
    return {str(a) for a in await Interview.get_motor_collection().distinct("accountId", q)}
