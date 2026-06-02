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
from app.models import Interview, ResumeApplication, Transaction, UserExtra, WeeklyPlan
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
            "image": getattr(u, "image", None) or None,
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


# ---------------------------------------------------------------------------
# Consolidated leaderboard — bids + interviews + conversion in one payload
# ---------------------------------------------------------------------------

_CONVERSION_MIN_BIDS = 10  # raised from 5 — needs more volume to qualify

_RANGE_LABELS = {"week": "This week", "30": "Last 30 days", "90": "Last 90 days", "365": "Last year"}


def _current_iso_week_start() -> tuple[datetime, int, int]:
    """Monday 00:00 UTC of the current ISO week + (iso year, iso week)."""
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    iso_year, iso_week, _ = monday.isocalendar()
    return monday, iso_year, iso_week


def _resolve_lb_range(range_str: str) -> tuple[datetime, datetime, datetime, datetime, str]:
    """Parse the range query into (start, end, prev_start, prev_end, label).
    Accepts 'week' (current ISO Mon–now) or an int day-count."""
    now = datetime.now(timezone.utc)
    range_str = (range_str or "week").strip().lower()
    if range_str == "week":
        start, _, _ = _current_iso_week_start()
        end = now
        length = end - start
        prev_end = start
        prev_start = prev_end - length
        return start, end, prev_start, prev_end, "This week"
    try:
        days = int(range_str)
    except ValueError:
        days = 7
    days = max(1, min(365, days))
    start = now - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    prev_end = start
    label = _RANGE_LABELS.get(str(days), f"Last {days} days")
    return start, now, prev_start, prev_end, label


async def _trend_by_user(user_ids: list, weeks: int = 8) -> dict:
    """Per-user trend of last N ISO weeks: [{label, bids, interviews}]."""
    if not user_ids:
        return {}
    now = datetime.now(timezone.utc)
    monday, _, _ = _current_iso_week_start()
    start = monday - timedelta(weeks=weeks - 1)

    bid_pipe = [
        {"$match": {"userId": {"$in": user_ids}, "createdAt": {"$gte": start}}},
        {"$group": {
            "_id": {"u": "$userId", "y": {"$isoWeekYear": "$createdAt"}, "w": {"$isoWeek": "$createdAt"}},
            "n": {"$sum": 1},
        }},
    ]
    iv_pipe = [
        {"$match": {"createdBy": {"$in": user_ids}, "scheduledAt": {"$gte": start}}},
        {"$group": {
            "_id": {"u": "$createdBy", "y": {"$isoWeekYear": "$scheduledAt"}, "w": {"$isoWeek": "$scheduledAt"}},
            "n": {"$sum": 1},
        }},
    ]
    bid_docs = await ResumeApplication.aggregate(bid_pipe).to_list()
    iv_docs = await Interview.aggregate(iv_pipe).to_list()

    # Build week buckets in order (oldest -> newest).
    buckets: list[tuple[int, int]] = []
    cursor = monday - timedelta(weeks=weeks - 1)
    for _ in range(weeks):
        y, w, _d = cursor.isocalendar()
        buckets.append((y, w))
        cursor += timedelta(weeks=1)

    out: dict = {}
    for u in user_ids:
        series = []
        for (y, w) in buckets:
            series.append({"label": f"W{w}", "year": y, "week": w, "bids": 0, "interviews": 0})
        out[u] = series

    for d in bid_docs:
        u = d["_id"]["u"]; y = d["_id"]["y"]; w = d["_id"]["w"]
        s = out.get(u)
        if not s:
            continue
        for row in s:
            if row["year"] == y and row["week"] == w:
                row["bids"] += int(d["n"])
                break
    for d in iv_docs:
        u = d["_id"]["u"]; y = d["_id"]["y"]; w = d["_id"]["w"]
        s = out.get(u)
        if not s:
            continue
        for row in s:
            if row["year"] == y and row["week"] == w:
                row["interviews"] += int(d["n"])
                break
    # Drop year/week scaffolding from output rows (frontend only needs label).
    for u, series in out.items():
        out[u] = [{"label": r["label"], "bids": r["bids"], "interviews": r["interviews"]} for r in series]
    return out


async def _weekly_targets_by_user(user_ids: list, year: int, week: int) -> dict:
    """Read each user's WeeklyPlan for (year, week). Pull bids+interviews
    target from the stored metrics list. Returns dict[user_id] ->
    {bidsTarget, interviewsTarget}."""
    if not user_ids:
        return {}
    plans = await WeeklyPlan.find({
        "userId": {"$in": user_ids},
        "year": year,
        "weekNumber": week,
    }).to_list()
    out: dict = {u: {"bidsTarget": 0, "interviewsTarget": 0} for u in user_ids}
    for p in plans:
        for m in (p.metrics or []):
            if m.key == "bids":
                out[p.userId]["bidsTarget"] = int(m.target or 0)
            elif m.key == "interviews":
                out[p.userId]["interviewsTarget"] = int(m.target or 0)
    return out


@router.get("/leaderboard/consolidated")
async def leaderboard_consolidated(
    user: Annotated[UserExtra, Depends(get_current_user)],
    range: str = Query("week"),
    trendWeeks: int = Query(8, ge=4, le=26),
) -> dict:
    """Single-payload leaderboard focused on bids, interviews, conversion.
    Pulls weekly targets (when scope = current week) so the table can show
    % of plan. Includes per-user 8-week trend + previous-period deltas +
    champion (#1) per metric + a personal stats block for the caller."""
    start, end, prev_start, prev_end, label = _resolve_lb_range(range)

    opt_in_users = await UserExtra.find({"role": {"$ne": "admin"}}).to_list()
    user_ids = [u.id for u in opt_in_users]

    cur_bids = await _bids_by_user(user_ids, start, end)
    cur_iv = await _interviews_by_user(user_ids, start, end)
    prev_bids = await _bids_by_user(user_ids, prev_start, prev_end)
    prev_iv = await _interviews_by_user(user_ids, prev_start, prev_end)
    trend = await _trend_by_user(user_ids, weeks=trendWeeks)

    # Targets only meaningful for the current-week scope.
    targets: dict = {}
    if range == "week":
        _monday, iso_year, iso_week = _current_iso_week_start()
        targets = await _weekly_targets_by_user(user_ids, iso_year, iso_week)

    rows: list[dict] = []
    for u in opt_in_users:
        b = int(cur_bids.get(u.id, 0))
        i = int(cur_iv.get(u.id, 0))
        pb = int(prev_bids.get(u.id, 0))
        pi = int(prev_iv.get(u.id, 0))
        conv = (i / b) if b >= _CONVERSION_MIN_BIDS else 0.0
        prev_conv = (pi / pb) if pb >= _CONVERSION_MIN_BIDS else 0.0
        t = targets.get(u.id, {"bidsTarget": 0, "interviewsTarget": 0})

        rows.append({
            "userId": str(u.id),
            "name": _display_name(u),
            "image": getattr(u, "image", None) or None,
            "bids": b,
            "interviews": i,
            "conversion": round(conv, 4),
            "qualifiesConversion": b >= _CONVERSION_MIN_BIDS,
            "bidsTarget": int(t.get("bidsTarget") or 0),
            "interviewsTarget": int(t.get("interviewsTarget") or 0),
            "prevBids": pb,
            "prevInterviews": pi,
            "prevConversion": round(prev_conv, 4),
            "trend": trend.get(u.id, []),
        })

    # Rank by each metric independently — frontend renders rank chips per cell.
    def _attach_ranks(rows: list[dict], key: str, *, qualifier: Optional[str] = None) -> None:
        contestants = [r for r in rows if (qualifier is None or r.get(qualifier))]
        contestants.sort(key=lambda r: r[key], reverse=True)
        for rank, r in enumerate(contestants, start=1):
            r[f"rank_{key}"] = rank

    _attach_ranks(rows, "bids")
    _attach_ranks(rows, "interviews")
    _attach_ranks(rows, "conversion", qualifier="qualifiesConversion")

    def _champ(key: str) -> Optional[dict]:
        ranked = [r for r in rows if f"rank_{key}" in r]
        if not ranked:
            return None
        winner = min(ranked, key=lambda r: r[f"rank_{key}"])
        return {"userId": winner["userId"], "name": winner["name"], "image": winner["image"], "value": winner[key]}

    champions = {
        "bids": _champ("bids"),
        "interviews": _champ("interviews"),
        "conversion": _champ("conversion"),
    }

    your_row = next((r for r in rows if r["userId"] == str(user.id)), None)
    your_stats = None
    if your_row is not None and user.role != "admin":
        your_stats = {
            "bids": your_row["bids"],
            "interviews": your_row["interviews"],
            "conversion": your_row["conversion"],
            "rankBids": your_row.get("rank_bids"),
            "rankInterviews": your_row.get("rank_interviews"),
            "rankConversion": your_row.get("rank_conversion"),
            "bidsTarget": your_row["bidsTarget"],
            "interviewsTarget": your_row["interviewsTarget"],
            "prevBids": your_row["prevBids"],
            "prevInterviews": your_row["prevInterviews"],
            "prevConversion": your_row["prevConversion"],
        }

    return {
        "range": range,
        "label": label,
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "conversionMinBids": _CONVERSION_MIN_BIDS,
        "champions": champions,
        "users": rows,
        "yourStats": your_stats,
    }


@router.get("/dashboard-feed")
async def dashboard_feed(
    user: Annotated[UserExtra, Depends(get_current_user)],
    limit: int = Query(10, ge=1, le=50),
    upcoming: int = Query(5, ge=1, le=20),
) -> dict:
    """Compact dashboard feed for the logged-in user:
      - `recent`: last N events (bid submitted, interview scheduled,
        interview completed) merged + sorted by timestamp.
      - `upcoming`: next M scheduled interviews from now forward.
    Admins see global aggregates (own user's events still); UI mirrors
    layout."""
    uid = user.id

    # Recent bids
    bids = await ResumeApplication.find({"userId": uid}).sort("-createdAt").limit(limit).to_list()
    bid_events = [{
        "kind": "bid",
        "at": (b.createdAt or datetime.now(timezone.utc)).isoformat(),
        "company": b.companyName,
        "profile": b.profileName,
        "jobId": str(b.id),
        "status": b.status,
    } for b in bids]

    # Interview events (scheduled + completed)
    ivs = await Interview.find({"createdBy": uid}).sort("-scheduledAt").limit(limit * 2).to_list()
    iv_events: list[dict] = []
    now = datetime.now(timezone.utc)
    for iv in ivs:
        sched = iv.scheduledAt
        if sched and sched <= now:
            iv_events.append({
                "kind": "interview_done" if iv.status and str(iv.status) in ("completed", "passed", "failed") else "interview_past",
                "at": sched.isoformat(),
                "company": iv.companyName,
                "stage": str(iv.stage) if iv.stage else None,
                "status": str(iv.status) if iv.status else None,
                "interviewId": str(iv.id),
            })

    merged = sorted([*bid_events, *iv_events], key=lambda x: x["at"], reverse=True)[:limit]

    # Upcoming interviews from now forward
    upcoming_ivs = await Interview.find({
        "createdBy": uid,
        "scheduledAt": {"$gte": now},
    }).sort("+scheduledAt").limit(upcoming).to_list()
    upcoming_out = [{
        "interviewId": str(iv.id),
        "company": iv.companyName,
        "stage": str(iv.stage) if iv.stage else None,
        "scheduledAt": iv.scheduledAt.isoformat() if iv.scheduledAt else None,
        "endsAt": iv.endsAt.isoformat() if iv.endsAt else None,
    } for iv in upcoming_ivs]

    return {
        "recent": merged,
        "upcoming": upcoming_out,
    }
