"""Admin AI spend report over the ai_usage_events ledger (app/ai_usage.py writes it, the backfill script
seeds history).

The SPA sends the period as UTC instants (`from` inclusive, `to` exclusive) plus the viewer's IANA time
zone, and buckets are labelled in that zone. Bucketing happens here rather than in Mongo: the event
volume is small (thousands a month) and mongomock, which the unit tests run on, can't do time zones."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.deps import require_admin
from app.models import AiUsageEvent, UserExtra

router = APIRouter(prefix="/ai-usage", tags=["ai-usage"])

MAX_SPAN = timedelta(days=366)
MAX_HOUR_BUCKETS = 72
HOURLY_UP_TO = timedelta(days=2)  # default bucket: hour up to two days, day beyond
UNATTRIBUTED = "unattributed"  # userId filter / option id for events with no user

_FIELDS = {
    "createdAt": 1, "userId": 1, "feature": 1, "model": 1, "inputTokens": 1, "outputTokens": 1,
    "costUsd": 1, "calls": 1, "estimated": 1,
}


def _instant(raw: str, name: str) -> datetime:
    """ISO instant → naive UTC (how createdAt is stored)."""
    try:
        value = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be an ISO date-time")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _zone(raw: str) -> ZoneInfo:
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail="tz must be an IANA time zone")


def _local(at: datetime, tz: ZoneInfo) -> datetime:
    return at.replace(tzinfo=timezone.utc).astimezone(tz)


def _label(at: datetime, tz: ZoneInfo, bucket: str) -> str:
    local = _local(at, tz)
    return local.strftime("%Y-%m-%dT%H:00") if bucket == "hour" else local.strftime("%Y-%m-%d")


def _bucket_labels(start: datetime, end: datetime, tz: ZoneInfo, bucket: str) -> list[str]:
    """Every bucket in [start, end), in order (a DST repeat hour appears once)."""
    labels: list[str] = []
    if bucket == "hour":
        at = start
        while at < end:
            label = _label(at, tz, "hour")
            if not labels or labels[-1] != label:
                labels.append(label)
            at += timedelta(hours=1)
        return labels
    day: date = _local(start, tz).date()
    last: date = _local(end - timedelta(microseconds=1), tz).date()
    while day <= last:
        labels.append(day.isoformat())
        day += timedelta(days=1)
    return labels


def _user_key(user_id) -> str:
    return str(user_id) if user_id is not None else UNATTRIBUTED


async def _names(keys: set[str]) -> dict[str, str]:
    from beanie import PydanticObjectId

    ids = [PydanticObjectId(k) for k in keys if k != UNATTRIBUTED]
    users = await UserExtra.find({"_id": {"$in": ids}}).to_list() if ids else []
    names = {str(u.id): (u.name or u.email or str(u.id)) for u in users}
    names[UNATTRIBUTED] = "Unattributed"
    return {k: names.get(k, k) for k in keys}  # a deleted user keeps showing their id


@router.get("")
async def ai_usage_report(
    _admin: Annotated[UserExtra, Depends(require_admin)],
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    tz: str = Query("UTC"),
    userId: Optional[list[str]] = Query(None),
    feature: Optional[list[str]] = Query(None),
    model: Optional[list[str]] = Query(None),
    bucket: Optional[Literal["hour", "day"]] = Query(None),
) -> dict:
    start, end, zone = _instant(from_, "from"), _instant(to, "to"), _zone(tz)
    if start >= end:
        raise HTTPException(status_code=422, detail="from must be before to")
    if end - start > MAX_SPAN:
        raise HTTPException(status_code=422, detail="the range can span at most 366 days")
    bucket = bucket or ("hour" if end - start <= HOURLY_UP_TO else "day")
    if bucket == "hour" and end - start > timedelta(hours=MAX_HOUR_BUCKETS):
        raise HTTPException(status_code=422, detail="hourly buckets cover at most 3 days")

    cursor = AiUsageEvent.get_motor_collection().find({"createdAt": {"$gte": start, "$lt": end}}, _FIELDS)
    docs = await cursor.to_list(length=None)

    # Options come from the whole range, so picking one user doesn't empty the other dropdowns.
    option_users = {_user_key(d.get("userId")) for d in docs}
    names = await _names(option_users)
    users_f, features_f, models_f = set(userId or []), set(feature or []), set(model or [])

    labels = _bucket_labels(start, end, zone, bucket)
    series = {label: {"costUsd": 0.0, "calls": 0, "byFeature": defaultdict(float), "byModel": defaultdict(float),
                      "byUser": defaultdict(float)} for label in labels}
    rows: dict[tuple[str, str, str], dict] = {}
    totals = {"costUsd": 0.0, "calls": 0, "inputTokens": 0, "outputTokens": 0, "estimatedCostUsd": 0.0,
              "unpricedCalls": 0}

    for d in docs:
        ukey, feat, mdl = _user_key(d.get("userId")), d.get("feature") or "other", d.get("model") or ""
        if (users_f and ukey not in users_f) or (features_f and feat not in features_f) or (models_f and mdl not in models_f):
            continue
        cost = d.get("costUsd")
        calls = int(d.get("calls") or 1)
        tin, tout = int(d.get("inputTokens") or 0), int(d.get("outputTokens") or 0)
        totals["calls"] += calls
        totals["inputTokens"] += tin
        totals["outputTokens"] += tout
        if cost is None:
            totals["unpricedCalls"] += calls
            cost = 0.0
        totals["costUsd"] += cost
        if d.get("estimated"):
            totals["estimatedCostUsd"] += cost

        point = series.get(_label(d["createdAt"], zone, bucket))
        if point is not None:
            point["costUsd"] += cost
            point["calls"] += calls
            point["byFeature"][feat] += cost
            point["byModel"][mdl] += cost
            point["byUser"][ukey] += cost

        row = rows.setdefault((ukey, feat, mdl), {
            "userId": None if ukey == UNATTRIBUTED else ukey, "userName": names[ukey], "feature": feat, "model": mdl,
            "calls": 0, "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0, "estimated": False,
        })
        row["calls"] += calls
        row["inputTokens"] += tin
        row["outputTokens"] += tout
        row["costUsd"] += cost
        row["estimated"] = row["estimated"] or bool(d.get("estimated"))

    def money(value: float) -> float:
        return round(value, 6)

    return {
        "from": start.replace(tzinfo=timezone.utc).isoformat(),
        "to": end.replace(tzinfo=timezone.utc).isoformat(),
        "tz": tz,
        "bucket": bucket,
        "totals": {**totals, "costUsd": money(totals["costUsd"]), "estimatedCostUsd": money(totals["estimatedCostUsd"])},
        "series": [
            {"bucket": label, "costUsd": money(p["costUsd"]), "calls": p["calls"],
             **{k: {key: money(v) for key, v in p[k].items()} for k in ("byFeature", "byModel", "byUser")}}
            for label, p in series.items()
        ],
        "rows": sorted(
            ({**r, "costUsd": money(r["costUsd"])} for r in rows.values()),
            key=lambda r: (-r["costUsd"], -r["calls"], r["userName"], r["feature"], r["model"]),
        ),
        "options": {
            "users": sorted(
                ({"id": k, "name": names[k]} for k in option_users),
                key=lambda u: (u["id"] == UNATTRIBUTED, u["name"].lower()),
            ),
            "features": sorted({d.get("feature") or "other" for d in docs}),
            "models": sorted({d.get("model") or "" for d in docs}),
        },
    }
