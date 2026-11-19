"""DB reads for the Statistics "Your profiles" report and the coverage line (the `applications` table)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.applications.rows import interview_outcome
from app.models import Application, Interview

_ROW_FIELDS = ("key", "accountId", "ownerId", "appliedAt", "by", "bidStatus", "interviewId", "daysToInterview",
               "furthest", "firstRoundAt")


async def load_period_rows(start: datetime, end: datetime, region: str) -> list[dict]:
    q: dict = {"appliedAt": {"$gte": start, "$lt": end}}
    if region != "all":
        q["region"] = region
    return await Application.get_motor_collection().find(q, {f: 1 for f in _ROW_FIELDS}).to_list(None)


async def profile_activity(account_ids: list) -> dict[str, dict]:
    """{accountId: {firstAt, lastAt, lastRoundAt}} over the profile's non-rejected rows, all time."""
    if not account_ids:
        return {}
    cur = Application.get_motor_collection().aggregate([
        {"$match": {"accountId": {"$in": account_ids}, "bidStatus": {"$ne": "rejected"}}},
        {"$group": {"_id": "$accountId", "firstAt": {"$min": "$appliedAt"}, "lastAt": {"$max": "$appliedAt"},
                    "lastRoundAt": {"$max": "$firstRoundAt"}}},
    ])
    return {str(d["_id"]): {k: d.get(k) for k in ("firstAt", "lastAt", "lastRoundAt")} async for d in cur}


async def count_unmatched_interviews(since: datetime, until: Optional[datetime] = None, owner_id=None) -> int:
    """Interviews created in [since, until) (optionally by one owner) that count and that no row of the table carries."""
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
    return sum(1 for d in ivs if d["_id"] not in carried and interview_outcome(Interview.model_validate(d)))


async def subject_unmatched_interviews(owner_id, start: datetime, end: datetime) -> int:
    return await count_unmatched_interviews(start, end, owner_id)
