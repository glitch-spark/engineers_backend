"""DB reads for the Statistics "Your profiles" report, the coverage line (the `applications` table) and the
Interviews report."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional

from app.applications.rows import interview_outcome
from app.models import Application, Interview
from app.stats.interview_report import caller_index, interview_facts

_ROW_FIELDS = ("key", "accountId", "ownerId", "appliedAt", "by", "bidStatus", "interviewId", "daysToInterview",
               "furthest", "firstRoundAt", "board", "scoreBand", "score", "area", "profileAgeDays", "bidderId",
               "templateName", "byInterviewOnly")
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


async def cached(ck: tuple, load, ttl: float = _TTL):
    """`load()`'s result, shared for `ttl` seconds by every caller of the key `ck`."""
    now = time.monotonic()
    entry = _cache.get(ck)
    if not entry or now - entry[0] >= ttl:
        for k in [k for k, (t, _) in _cache.items() if now - t >= _TTL]:
            del _cache[k]
        while len(_cache) >= _MAX:
            del _cache[min(_cache, key=lambda k: _cache[k][0])]
        entry = _cache[ck] = (now, asyncio.ensure_future(load()))
    try:
        return await asyncio.shield(entry[1])  # one caller cancelled (client gone) must not cancel the others
    except Exception:
        if _cache.get(ck) is entry:
            del _cache[ck]
        raise


async def load_period_rows(start: datetime, end: datetime, region: str) -> list[dict]:
    return await cached((start, end, region), lambda: _fetch(start, end, region))


# what interview_report.interview_facts reads; never the transcripts and notes
_ROUND_FIELDS = ("id", "stage", "at", "scheduledAt", "endsAt", "status", "statusReason", "statusAt", "reschedules",
                 "followUps", "caller")
_INTERVIEW_FIELDS = ("accountId", "createdBy", "scheduledAt", "endsAt", "stage", "status", "caller", "appliedBy",
                     "fitScore", "resumeLink.kind", "resumeLink.score", "createdAt", "updatedAt",
                     *(f"stageHistory.{f}" for f in _ROUND_FIELDS))


async def _fetch_interviews(start: datetime, end: datetime) -> list[dict]:
    # a round in [start, end) is a round near it: the day of slack covers timezone-free naive dates
    near = {"$gte": start - timedelta(days=1), "$lt": end + timedelta(days=1)}
    docs = await Interview.get_motor_collection().find(
        {"$or": [{"scheduledAt": near}, {"stageHistory.scheduledAt": near}]}, {f: 1 for f in _INTERVIEW_FIELDS},
    ).to_list(None)
    apps: dict = {}
    for a in await Application.get_motor_collection().find(
            {"interviewId": {"$in": [d["_id"] for d in docs]}}, {f: 1 for f in ("interviewId", "by", "scoreBand", "area")},
    ).to_list(None):
        apps.setdefault(a["interviewId"], a)
    from app.caller_directory import list_directory

    callers, now = caller_index(await list_directory(include_archived=True)), datetime.utcnow()
    return [interview_facts(Interview.model_validate(d), d["_id"], apps.get(d["_id"]), now, callers) for d in docs]


async def load_interview_facts(start: datetime, end: datetime) -> list[dict]:
    """Statistics → Interviews: every interview with a round near [start, end), as interview_report.interview_facts.
    One cached load per period for every region and scope (they filter by profile in memory)."""
    return await cached(("interviews", start, end), lambda: _fetch_interviews(start, end))


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


async def interviews_started(match: dict, start: datetime, end: datetime, skip: frozenset = frozenset()) -> list[dict]:
    """Projected interviews matching `match` (ids in `skip` left out) whose first countable round is in [start, end)."""
    # a first round in [start, end) implies a round near it: the day of slack covers timezone-free naive dates
    near = {"$gte": start - timedelta(days=1), "$lt": end + timedelta(days=1)}
    ivs = await Interview.get_motor_collection().find(
        {**match, "$or": [{"scheduledAt": near}, {"stageHistory.scheduledAt": near}]},
        {f: 1 for f in ("accountId", "createdBy", "scheduledAt", "stage", "status", "stageHistory", "createdAt", "updatedAt")},
    ).to_list(None)
    out = []
    for d in ivs:
        if d["_id"] in skip:
            continue
        o = interview_outcome(Interview.model_validate(d))
        if o and o["firstRoundAt"] and start <= o["firstRoundAt"] < end:
            out.append(d)
    return out


async def count_untied_interviews(account_ids: list, start: datetime, end: datetime) -> int:
    """Interviews on `account_ids` whose first countable round is in [start, end) and that no row carries."""
    if not account_ids:
        return 0
    carried = frozenset(await Application.get_motor_collection().distinct("interviewId"))
    return len(await interviews_started({"accountId": {"$in": list(account_ids)}}, start, end, carried))


async def accounts_with_rounds_since(account_ids: list, since: datetime) -> set[str]:
    """Profiles (of `account_ids`) with any interview round scheduled at or after `since`, matched to a row or not."""
    if not account_ids:
        return set()
    q = {"accountId": {"$in": list(account_ids)},
         "$or": [{"stageHistory.scheduledAt": {"$gte": since}}, {"scheduledAt": {"$gte": since}}]}
    return {str(a) for a in await Interview.get_motor_collection().distinct("accountId", q)}
