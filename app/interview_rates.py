"""Statistics → Interview rate: every user's bids, interviews and bid→interview rate for one week, month or custom
range, in one region or all of them, plus each user's bids and interviews per day, week or month of it.

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

Period = Literal["week", "month", "custom"]
Unit = Literal["day", "week", "month"]
Region = Literal["all", "Latam", "EU", "US"]
REGIONS: tuple[str, ...] = ("Latam", "EU", "US")
MIN_BIDS = 30  # fewer bids than this in the period: the rate is too noisy to rank
MAX_CUSTOM_DAYS = 366


# Applies typed in a daily plan's Done, per region; None = all of them.
_BID_FIELD = {"Latam": "bidsSelfLatam", "EU": "bidsSelfEu", "US": "bidsSelfUs"}
# Where bidder applies count: bidders only apply to US jobs.
BIDDER_REGIONS = ("all", "US")


def window(period: Period, day: date, until: Optional[date] = None) -> tuple[date, date]:
    """First and last local day of the week (Monday–Sunday, as the weekly plans) or calendar month holding `day`;
    for a custom range, `day` to `until`."""
    if period == "custom":
        return day, until or day
    if period == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    return date(day.year, day.month, 1), date(day.year, day.month, monthrange(day.year, day.month)[1])


def label(period: Period, start: date, end: date) -> str:
    if period == "month":
        return start.strftime("%b %Y")
    if start == end:
        return f"{start:%b} {start.day}, {start.year}"
    if start.year != end.year:
        return f"{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}"
    if start.month == end.month:
        return f"{start:%b} {start.day}–{end.day}, {end.year}"
    return f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}"


def unit(period: Period, start: date, end: date) -> Unit:
    """The per-user charts' bars: days of a week, weeks of a month; a custom range by its length."""
    if period == "week":
        return "day"
    if period == "month":
        return "week"
    n = (end - start).days + 1
    return "day" if n <= 14 else "week" if n <= 120 else "month"


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


async def counts(
    user_ids: list, start: date, end: date, region: Region, regions: dict, days: Optional[dict] = None,
) -> dict:
    """userId → [bids, interviews, bidder bids] over the local days [start, end], in the selected region unless
    `all`. Bids include the bidder ones (All and US only: see BIDDER_REGIONS). With `days`, also fills
    (day, userId) → [self bids, bidder bids, interviews] for the per-user charts."""
    from app.dashboard_stats import app_tz, local_day_start_utc
    from app.interview_rounds import count_interview_rounds, load_interviews_for_window, split_rounds_by_day
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
        own = getattr(p.done, field) if field else p.done.bidsSelf
        out[p.userId][0] += own + bidder
        out[p.userId][2] += bidder
        if days is not None:
            d = days.setdefault((p.date.date(), p.userId), [0, 0, 0])
            d[0] += own
            d[1] += bidder
    tz = app_tz()
    first, last = local_day_start_utc(start, tz), local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    for iv in await load_interviews_for_window(user_ids, first, last):
        if region == "all" or regions.get(iv.accountId) == region:
            out[iv.createdBy][1] += count_interview_rounds(iv, first, last)[0]
            if days is not None:
                for day, *_ in split_rounds_by_day(iv, first, last, tz):
                    days.setdefault((day, iv.createdBy), [0, 0, 0])[2] += 1
    return out


def buckets(unit: Unit, start: date, end: date) -> list[tuple[date, date, str]]:
    """Each day, Monday–Sunday week or calendar month of [start, end], clipped to it, with its label."""
    if unit == "day":
        return [(d, d, f"{d:%a} {d.day}") for d in (start + timedelta(days=i) for i in range((end - start).days + 1))]
    out, s = [], start
    while s <= end:
        if unit == "week":
            e = min(s + timedelta(days=6 - s.weekday()), end)
            lbl = f"{s:%b} {s.day}" if s == e else f"{s:%b} {s.day}–{e.day}" if s.month == e.month \
                else f"{s:%b} {s.day} – {e:%b} {e.day}"
        else:
            e = min(date(s.year, s.month, monthrange(s.year, s.month)[1]), end)
            lbl = s.strftime("%b %Y")
        out.append((s, e, lbl))
        s = e + timedelta(days=1)
    return out


def per_user_chart(unit: Unit, start: date, end: date, users: list, days: dict, viewer_id, parts: dict) -> dict:
    """`parts` (name → index in `days`' values) per user per `unit`: the bids chart's self and
    bidder applies, the interviews chart's rounds. `users` are every ranked user, oldest account first: a user's
    color slot is their place there, so it doesn't change with the chart, period or region. Users with none in the
    period are left out."""
    from app.routers.metrics import _display_name

    totals: dict = defaultdict(int)
    for (_, uid), v in days.items():
        totals[uid] += sum(v[i] for i in parts.values())
    shown = [(slot, u) for slot, u in enumerate(users) if totals.get(u.id)]
    out = []
    for s, e, lbl in buckets(unit, start, end):
        values = {str(u.id): dict.fromkeys(parts, 0) for _, u in shown}
        for (d, uid), v in days.items():
            if s <= d <= e and str(uid) in values:
                for name, i in parts.items():
                    values[str(uid)][name] += v[i]
        out.append({"label": lbl, "start": s.isoformat(), "end": e.isoformat(), "values": values})
    return {
        "unit": unit,
        "users": [{"userId": str(u.id), "name": _display_name(u), "slot": slot, "isMe": u.id == viewer_id}
                  for slot, u in shown],
        "buckets": out,
    }


def ranked(rows: list[dict]) -> list[dict]:
    """Rate, highest first; users below MIN_BIDS after everyone ranked (rank None), then by bids."""
    rows.sort(key=lambda r: (r["lowVolume"], -(r["rate"] or 0), -r["bids"], r["name"].lower()))
    n = 0
    for r in rows:
        r["rank"] = None if r["lowVolume"] else (n := n + 1)
    return rows


async def interview_rates(viewer, period: Period, day: date, region: Region, until: Optional[date] = None) -> dict:
    from app.models import UserExtra
    from app.routers.metrics import _display_name

    users = await UserExtra.find({"role": {"$ne": "admin"}}).to_list()
    by_id = {u.id: u for u in users}
    user_ids = list(by_id)
    if viewer.id not in by_id:  # an admin still sees the board (not ranked in it)
        user_ids.append(viewer.id)
    regions, unassigned = await _profile_regions()

    start, end = window(period, day, until)
    days: dict = {}
    now = await counts(user_ids, start, end, region, regions, days)
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

    oldest_first = sorted(users, key=lambda u: u.id)
    chart_unit = unit(period, start, end)

    return {
        "period": period, "region": region, "start": start.isoformat(), "end": end.isoformat(),
        "label": label(period, start, end), "minBids": MIN_BIDS,
        "rows": ranked(rows),
        "totals": {"bids": team[0], "selfBids": team[0] - team[2], "bidderBids": team[2], "interviews": team[1],
                   "rate": rate(team[0], team[1])},
        # Bids shown as self (+bidder): All and US, where the bidder applies count.
        "bidderSplit": region in BIDDER_REGIONS,
        "bidsChart": per_user_chart(chart_unit, start, end, oldest_first, days, viewer.id, {"self": 0, "bidder": 1}),
        "interviewsChart": per_user_chart(chart_unit, start, end, oldest_first, days, viewer.id, {"interviews": 2}),
        "unassignedProfiles": unassigned,
    }
