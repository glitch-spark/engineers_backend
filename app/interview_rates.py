"""Statistics → Interview rate: every user's bids, interviews and bid→interview rate for one week or month, in one
region or all of them, plus the viewer's and the team's rate over the last periods.

Counted as the plans count them (app/plan_auto.py), on the app's local days:
- bids are the applies typed in daily plans' Done, on followed-up (logged) days only. All = self + bidder applies (the
  Leaderboard's "bids done"); a region = that region's self applies (bidsSelfUs / Eu / Latam), and US also the bidder
  applies: bidders only apply to US jobs. Self applies typed as a plain total (before the region split) have no
  region, so they count under All only;
- interviews are the rounds held (app/interview_rounds.py), in the region of the interview's profile.
Every non-admin user is ranked, as on the Leaderboard; below MIN_BIDS bids a rate is shown but not ranked.
"""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Literal, Optional

Period = Literal["week", "month"]
Region = Literal["all", "Latam", "EU", "US"]
REGIONS: tuple[str, ...] = ("Latam", "EU", "US")
MIN_BIDS = 30  # fewer bids than this in the period: the rate is too noisy to rank
TREND_PERIODS = 8


# Applies typed in a daily plan's Done, per region; None = all of them.
_BID_FIELD = {"Latam": "bidsSelfLatam", "EU": "bidsSelfEu", "US": "bidsSelfUs"}
# Where bidder applies count: bidders only apply to US jobs.
BIDDER_REGIONS = ("all", "US")


def window(period: Period, day: date) -> tuple[date, date]:
    """First and last local day of the week (Monday–Sunday, as the weekly plans) or calendar month holding `day`."""
    if period == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    return date(day.year, day.month, 1), date(day.year, day.month, monthrange(day.year, day.month)[1])


def previous(period: Period, start: date) -> date:
    """A day in the period before the one starting at `start`."""
    return start - timedelta(days=1)


def label(period: Period, start: date, end: date) -> str:
    if period == "month":
        return start.strftime("%b %Y")
    if start.month == end.month:
        return f"{start:%b} {start.day}–{end.day}, {end.year}"
    return f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}"


def rate(bids: int, interviews: int) -> Optional[float]:
    return round(interviews / bids, 4) if bids else None


async def _profile_regions() -> tuple[dict, int]:
    """accountId → region (None when not set), and how many profiles have none."""
    from app.models import Account

    regions = {
        d["_id"]: (d.get("region") or None)
        async for d in Account.get_motor_collection().find({}, {"region": 1})
    }
    return regions, sum(1 for r in regions.values() if not r)


async def counts(user_ids: list, start: date, end: date, region: Region, regions: dict) -> dict:
    """userId → [bids, interviews, bidder bids] over the local days [start, end], in the selected region unless
    `all`. Bids include the bidder ones (All and US only: see BIDDER_REGIONS)."""
    from app.dashboard_stats import app_tz, local_day_start_utc
    from app.interview_rounds import count_interview_rounds, load_interviews_for_window
    from app.models import DailyPlan

    out: dict = defaultdict(lambda: [0, 0, 0])
    field = _BID_FIELD.get(region)
    plans = await DailyPlan.find({
        "userId": {"$in": user_ids},
        "date": {"$gte": datetime(start.year, start.month, start.day), "$lte": datetime(end.year, end.month, end.day)},
        "loggedAt": {"$ne": None},  # a goal-only day hasn't been followed up: no bids yet (plan_auto.weekly_done)
    }).to_list()
    for p in plans:
        bidder = p.done.bidsBidder if region in BIDDER_REGIONS else 0
        out[p.userId][0] += (getattr(p.done, field) if field else p.done.bidsSelf) + bidder
        out[p.userId][2] += bidder
    tz = app_tz()
    first, last = local_day_start_utc(start, tz), local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    for iv in await load_interviews_for_window(user_ids, first, last):
        if region == "all" or regions.get(iv.accountId) == region:
            out[iv.createdBy][1] += count_interview_rounds(iv, first, last)[0]
    return out


def ranked(rows: list[dict]) -> list[dict]:
    """Rate, highest first; users below MIN_BIDS after everyone ranked (rank None), then by bids."""
    rows.sort(key=lambda r: (r["lowVolume"], -(r["rate"] or 0), -r["bids"], r["name"].lower()))
    n = 0
    for r in rows:
        r["rank"] = None if r["lowVolume"] else (n := n + 1)
    return rows


async def interview_rates(viewer, period: Period, day: date, region: Region) -> dict:
    from app.models import UserExtra
    from app.routers.metrics import _display_name

    users = await UserExtra.find({"role": {"$ne": "admin"}}).to_list()
    by_id = {u.id: u for u in users}
    user_ids = list(by_id)
    if viewer.id not in by_id:  # an admin still sees the board (not ranked in it)
        user_ids.append(viewer.id)
    regions, unassigned = await _profile_regions()

    start, end = window(period, day)
    now = await counts(user_ids, start, end, region, regions)
    rows = []
    for uid in by_id:
        bids, interviews, bidder = now.get(uid, (0, 0, 0))
        if not (bids or interviews) and uid != viewer.id:
            continue  # nothing in this period and region: left off the board
        u = by_id[uid]
        rows.append({
            "userId": str(uid), "name": _display_name(u), "image": getattr(u, "image", None) or None,
            "bids": bids, "selfBids": bids - bidder, "bidderBids": bidder,
            "interviews": interviews, "rate": rate(bids, interviews),
            "lowVolume": bids < MIN_BIDS, "isMe": uid == viewer.id,
        })
    team = [sum(v[i] for k, v in now.items() if k in by_id) for i in range(3)]

    trend = []
    cursor = day
    for _ in range(TREND_PERIODS):
        s, e = window(period, cursor)
        c = now if s == start else await counts(user_ids, s, e, region, regions)
        mine = c.get(viewer.id, (0, 0, 0))[:2]
        tb = sum(v[0] for k, v in c.items() if k in by_id)
        ti = sum(v[1] for k, v in c.items() if k in by_id)
        trend.append({
            "label": s.strftime("%b %Y") if period == "month" else f"{s:%b} {s.day}",
            "start": s.isoformat(), "bids": mine[0], "interviews": mine[1], "rate": rate(*mine),
            "teamBids": tb, "teamInterviews": ti, "teamRate": rate(tb, ti),
        })
        cursor = previous(period, s)
    trend.reverse()

    return {
        "period": period, "region": region, "start": start.isoformat(), "end": end.isoformat(),
        "label": label(period, start, end), "minBids": MIN_BIDS,
        "rows": ranked(rows),
        "totals": {"bids": team[0], "selfBids": team[0] - team[2], "bidderBids": team[2], "interviews": team[1],
                   "rate": rate(team[0], team[1])},
        # Bids shown as self (+bidder): All and US, where the bidder applies count.
        "bidderSplit": region in BIDDER_REGIONS,
        "trend": trend,
        "unassignedProfiles": unassigned,
    }
