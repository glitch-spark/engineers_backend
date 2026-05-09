"""Dashboard metrics endpoint.

Single endpoint serves the four headline KPIs (income, bids, interviews,
bid→interview conversion) for a window plus the same totals for the prior
window of equal length so the frontend can show "vs previous period" deltas
without a second round-trip.

Bid = ResumeApplication. Income = sum of approved Transaction.amount.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.deps import get_current_user
from app.models import Interview, ResumeApplication, Transaction, UserExtra
from app.routers._util import parse_date

router = APIRouter(prefix="/metrics", tags=["metrics"])


def _window_for_range(days: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = now
    start = now - timedelta(days=days)
    return start, end


def _resolve_window(
    from_str: Optional[str],
    to_str: Optional[str],
    default_days: int,
) -> tuple[datetime, datetime]:
    if from_str and to_str:
        start = parse_date(from_str)
        end = parse_date(to_str)
        if not start or not end:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid from/to",
            )
        return start, end
    return _window_for_range(default_days)


async def _income_sum(user_id, start: datetime, end: datetime) -> float:
    """Sum of approved transactions in the window. Uses Transaction.date
    as the cash-flow timestamp (when income happened), not createdAt."""
    pipeline = [
        {"$match": {
            "userId": user_id,
            "status": "approved",
            "date": {"$gte": start, "$lte": end},
        }},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    docs = await Transaction.aggregate(pipeline).to_list()
    return float(docs[0]["total"]) if docs else 0.0


async def _bid_count(user_id, start: datetime, end: datetime) -> int:
    return await ResumeApplication.find(
        {"userId": user_id, "createdAt": {"$gte": start, "$lte": end}}
    ).count()


async def _interview_count(user_id, start: datetime, end: datetime) -> int:
    # Use scheduledAt: that's when the interview "happened" in user time.
    # createdAt is when the row was logged, often after the fact.
    return await Interview.find(
        {"createdBy": user_id, "scheduledAt": {"$gte": start, "$lte": end}}
    ).count()


_BUCKET_UNIT = {"day": "day", "week": "week", "month": "month"}


async def _bucketed_income(user_id, start, end, bucket: str) -> dict[str, float]:
    pipeline = [
        {"$match": {
            "userId": user_id,
            "status": "approved",
            "date": {"$gte": start, "$lte": end},
        }},
        {"$group": {
            "_id": {"$dateTrunc": {"date": "$date", "unit": _BUCKET_UNIT[bucket], "timezone": "UTC"}},
            "total": {"$sum": "$amount"},
        }},
    ]
    docs = await Transaction.aggregate(pipeline).to_list()
    return {d["_id"].isoformat(): float(d["total"]) for d in docs if d["_id"]}


async def _bucketed_count(coll, user_field: str, user_id, start, end, ts_field: str, bucket: str) -> dict[str, int]:
    pipeline = [
        {"$match": {
            user_field: user_id,
            ts_field: {"$gte": start, "$lte": end},
        }},
        {"$group": {
            "_id": {"$dateTrunc": {"date": f"${ts_field}", "unit": _BUCKET_UNIT[bucket], "timezone": "UTC"}},
            "n": {"$sum": 1},
        }},
    ]
    docs = await coll.aggregate(pipeline).to_list()
    return {d["_id"].isoformat(): int(d["n"]) for d in docs if d["_id"]}


def _bucket_starts(start: datetime, end: datetime, bucket: str) -> list[datetime]:
    """Generate the canonical list of bucket-start datetimes spanning [start, end]
    so empty buckets render as zeros instead of being omitted from the series."""
    out: list[datetime] = []
    if bucket == "day":
        cur = start.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=1)
    elif bucket == "week":
        # Truncate to ISO week start (Monday) in UTC.
        cur = start - timedelta(days=start.weekday())
        cur = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=7)
    else:  # month
        cur = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        step = None  # special-cased below
    while cur <= end:
        out.append(cur)
        if bucket == "month":
            year, month = cur.year, cur.month
            if month == 12:
                cur = cur.replace(year=year + 1, month=1)
            else:
                cur = cur.replace(month=month + 1)
        else:
            cur = cur + step  # type: ignore[operator]
    return out


async def _series(user_id, start: datetime, end: datetime, bucket: str) -> list[dict]:
    income_map = await _bucketed_income(user_id, start, end, bucket)
    bid_map = await _bucketed_count(
        ResumeApplication, "userId", user_id, start, end, "createdAt", bucket
    )
    iv_map = await _bucketed_count(
        Interview, "createdBy", user_id, start, end, "scheduledAt", bucket
    )
    rows = []
    for b in _bucket_starts(start, end, bucket):
        key = b.replace(tzinfo=timezone.utc).isoformat() if b.tzinfo is None else b.isoformat()
        # Mongo dateTrunc emits naive UTC; normalize lookup.
        alt_key = b.replace(tzinfo=None).isoformat() + "+00:00"
        income = income_map.get(key) or income_map.get(alt_key) or 0.0
        bids = bid_map.get(key) or bid_map.get(alt_key) or 0
        ivs = iv_map.get(key) or iv_map.get(alt_key) or 0
        rate = (ivs / bids) if bids > 0 else 0.0
        rows.append({
            "bucketStart": b.isoformat(),
            "income": round(float(income), 2),
            "bids": int(bids),
            "interviews": int(ivs),
            "rate": round(rate, 4),
        })
    return rows


async def _kpis(user_id, start: datetime, end: datetime) -> dict:
    income = await _income_sum(user_id, start, end)
    bids = await _bid_count(user_id, start, end)
    interviews = await _interview_count(user_id, start, end)
    rate = (interviews / bids) if bids > 0 else 0.0
    return {
        "income": round(income, 2),
        "bids": bids,
        "interviews": interviews,
        "bidToInterview": round(rate, 4),
    }


@router.get("/dashboard")
async def dashboard_metrics(
    user: Annotated[UserExtra, Depends(get_current_user)],
    range: int = Query(30, ge=1, le=365, description="Window length in days"),
    bucket: Literal["day", "week", "month"] = Query("day"),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    include_series: bool = Query(True, alias="includeSeries"),
) -> dict:
    """KPIs for the requested window + same-length prior window for deltas
    + per-bucket series for charting."""
    start, end = _resolve_window(from_, to, default_days=range)
    span = end - start
    prev_start = start - span
    prev_end = start

    totals = await _kpis(user.id, start, end)
    previous = await _kpis(user.id, prev_start, prev_end)
    series = await _series(user.id, start, end, bucket) if include_series else []
    return {
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "previousWindow": {"from": prev_start.isoformat(), "to": prev_end.isoformat()},
        "bucket": bucket,
        "totals": totals,
        "previousTotals": previous,
        "series": series,
    }


# ---------- leaderboard ---------------------------------------------------

LeaderboardMetric = Literal["earnings", "bids", "interviews", "conversion"]

_ANIMAL_POOL = [
    "fox", "owl", "wolf", "bear", "lion", "eagle", "panda", "tiger",
    "otter", "raven", "hawk", "lynx", "moose", "shark", "cobra", "bison",
]


def _pseudonym(user_id) -> str:
    """Stable, anonymized display label for opt-in users who hide identity."""
    h = hashlib.sha256(str(user_id).encode()).hexdigest()
    n = int(h[:8], 16)
    return f"Anonymous {_ANIMAL_POOL[n % len(_ANIMAL_POOL)]} #{n % 1000}"


def _display_name(u: UserExtra) -> str:
    if u.leaderboardAnon:
        return _pseudonym(u.id)
    return (u.leaderboardName or u.name or u.email or _pseudonym(u.id)).strip()


async def _earnings_by_user(user_ids, start, end) -> dict:
    pipeline = [
        {"$match": {
            "userId": {"$in": user_ids},
            "status": "approved",
            "date": {"$gte": start, "$lte": end},
        }},
        {"$group": {"_id": "$userId", "value": {"$sum": "$amount"}}},
    ]
    docs = await Transaction.aggregate(pipeline).to_list()
    return {d["_id"]: float(d["value"]) for d in docs}


async def _bids_by_user(user_ids, start, end) -> dict:
    pipeline = [
        {"$match": {
            "userId": {"$in": user_ids},
            "createdAt": {"$gte": start, "$lte": end},
        }},
        {"$group": {"_id": "$userId", "value": {"$sum": 1}}},
    ]
    docs = await ResumeApplication.aggregate(pipeline).to_list()
    return {d["_id"]: int(d["value"]) for d in docs}


async def _interviews_by_user(user_ids, start, end) -> dict:
    pipeline = [
        {"$match": {
            "createdBy": {"$in": user_ids},
            "scheduledAt": {"$gte": start, "$lte": end},
        }},
        {"$group": {"_id": "$createdBy", "value": {"$sum": 1}}},
    ]
    docs = await Interview.aggregate(pipeline).to_list()
    return {d["_id"]: int(d["value"]) for d in docs}


@router.get("/leaderboard")
async def leaderboard(
    user: Annotated[UserExtra, Depends(get_current_user)],
    metric: LeaderboardMetric = Query("earnings"),
    range: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Public leaderboard across users who opted in. Caller's own row is
    always returned in `yourRank` even if outside the top N (or if not
    opted in — useful so they can see what they'd rank at)."""
    start, end = _window_for_range(range)
    # Opt-in is enforced by role: every non-admin staff user is in, admins
    # are out. Stored leaderboardOptIn flag is ignored.
    opt_in_users = await UserExtra.find({"role": {"$ne": "admin"}}).to_list()
    user_ids = [u.id for u in opt_in_users]

    earnings = await _earnings_by_user(user_ids, start, end)
    bids_map = await _bids_by_user(user_ids, start, end)
    iv_map = await _interviews_by_user(user_ids, start, end)

    rows: list[dict] = []
    for u in opt_in_users:
        e = float(earnings.get(u.id, 0.0))
        b = int(bids_map.get(u.id, 0))
        i = int(iv_map.get(u.id, 0))
        # Min 5 bids to qualify for conversion ranking — stops 1/1 = 100% gaming.
        conv = (i / b) if (b >= 5) else 0.0

        if metric == "earnings":
            value = e
            secondary = f"{b} bids · {i} interviews"
        elif metric == "bids":
            value = float(b)
            secondary = f"${e:,.0f} · {i} interviews"
        elif metric == "interviews":
            value = float(i)
            secondary = f"{b} bids · ${e:,.0f}"
        else:  # conversion
            value = conv
            secondary = f"{i}/{b} bids" if b >= 5 else "needs 5+ bids"

        rows.append({
            "userId": str(u.id),
            "name": _display_name(u),
            "value": round(value, 4) if metric == "conversion" else round(value, 2),
            "secondary": secondary,
            "_qualifies": (b >= 5) if metric == "conversion" else True,
            "_b": b, "_i": i, "_e": e,
        })

    # Sort: qualifiers first (conversion-only filter), then by value desc.
    if metric == "conversion":
        rows = [r for r in rows if r["_qualifies"]]
    rows.sort(key=lambda r: r["value"], reverse=True)

    out_of = len(rows)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
        # Strip private helpers
        del row["_qualifies"]
        del row["_b"]; del row["_i"]; del row["_e"]

    your_rank = next((r for r in rows if r["userId"] == str(user.id)), None)

    return {
        "metric": metric,
        "range": range,
        "rows": rows[:limit],
        "yourRank": {
            "rank": your_rank["rank"] if your_rank else None,
            "value": your_rank["value"] if your_rank else None,
            "secondary": your_rank["secondary"] if your_rank else None,
            "outOf": out_of,
            "optedIn": user.role != "admin",
        },
    }
