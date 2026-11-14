"""Bids → Performance tab: how each bidder's bids add up over a week, month or custom range, on Bid Track days (see
the bidder performance spec §1).

The pure helpers (weekly columns, 50+ day streaks, the previous period, the top performer cards) come first, the
loaders and the response after them. A 50+ day is a weekday (Monday–Friday) with TARGET or more approved + pending
bids; weekends neither count nor break a streak.
"""
from __future__ import annotations

from typing import Optional

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from app.bids.records import records_start_utc
from app.bids.schemas import _iso_utc
from app.bids.week import DayWindow, _day_counts
from app.bids.windows import BidSchedule, period_key, schedule_from_settings, window_for
from app.dashboard_stats import app_tz, local_day_start_utc
from app.interview_rounds import split_rounds_by_day
from app.models import Account, Interview, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.profile_metrics import self_bids

TARGET = 50  # bids a weekday needs to be a 50+ day
MIN_BIDS_FOR_RATE = 100  # fewer bids than this: the bid→interview rate is too noisy for "Best conversion"


def week_columns(start: date, end: date) -> list[dict]:
    """Every Monday–Sunday week touching [start, end], clipped to it; `partial` when the range cuts the week."""
    out, s = [], start
    while s <= end:
        mon = s - timedelta(days=s.weekday())
        e = min(mon + timedelta(days=6), end)
        out.append({"start": s.isoformat(), "end": e.isoformat(), "partial": s != mon or e != mon + timedelta(days=6)})
        s = e + timedelta(days=1)
    return out


def day_columns(days: list[date]) -> list[dict]:
    """One column per report day (a pay week's)."""
    return [{"start": d.isoformat(), "end": d.isoformat(), "partial": False} for d in days]


def pay_week_columns(start: date, end: date, sched: BidSchedule) -> list[dict]:
    """Every pay week touching [start, end] (its report days), clipped to it; `partial` when the range cuts it."""
    from app.bids.week import pay_week

    out, d = [], start
    while d <= end:
        days = [date.fromisoformat(w.key) for w in pay_week(sched, datetime.now(timezone.utc), d).days]
        first, last = days[0], days[-1]
        s, e = max(first, start), min(last, end)
        out.append({"start": s.isoformat(), "end": e.isoformat(), "partial": s != first or e != last})
        d = last + timedelta(days=1)
    return out


def previous_range(period: str, start: date, end: date) -> tuple[date, date]:
    """The period to compare with: the week before; the month before, cut to the same number of days (clipped to its
    last day); a custom range's length of days just before it."""
    days = (end - start).days + 1
    if period == "week":
        return start - timedelta(days=7), end - timedelta(days=7)
    if period == "month":
        first = (start.replace(day=1) - timedelta(days=1)).replace(day=1)
        return first, min(first + timedelta(days=days - 1), date(first.year, first.month, monthrange(first.year, first.month)[1]))
    return start - timedelta(days=days), start - timedelta(days=1)


def streaks(counts: dict[date, int], first: date, last: date, today: date) -> dict:
    """Longest and current run of 50+ weekdays, hit days and weekdays counted over [first, last]. Today only counts
    once it has hit the target: it isn't over, so it can't miss yet."""
    stop = min(last, today - timedelta(days=1))
    days = [first + timedelta(days=i) for i in range((stop - first).days + 1)]
    if first <= today <= last and counts.get(today, 0) >= TARGET:
        days.append(today)
    longest = run = hits = n = 0
    for d in days:
        if d.weekday() >= 5:
            continue
        n += 1
        if counts.get(d, 0) >= TARGET:
            hits += 1
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return {"longestStreak": longest, "currentStreak": run, "hitDays": hits, "weekdays": n}


def day_strip(counts: dict[date, int], start: date, end: date, first: date, last: date, today: date) -> list[dict]:
    """One entry per weekday of [start, end] with its bids and state: "hit" (50+), "miss", "open" (today, under 50
    so far) or "none" (the bidder didn't exist yet, was archived, or before bid records). Same rules as `streaks`."""
    out = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        n = counts.get(d, 0)
        if d < first or d > last or d > today:
            state = "none"
        elif n >= TARGET:
            state = "hit"
        else:
            state = "open" if d == today else "miss"
        out.append({"day": d.isoformat(), "bids": n, "state": state})
    return out


def rank(rows: list[dict]) -> dict:
    """Top 3 bidder ids for each card; a row with 0 or no value for a card isn't on it."""
    name = lambda r: r["name"].lower()

    def top(key, ties, pool=rows):
        have = [r for r in pool if r.get(key)]
        have.sort(key=lambda r: (-r[key], *ties(r)))
        return [r["id"] for r in have[:3]]

    return {
        "bids": top("total", lambda r: (-r["approved"], name(r))),
        "streak": top("longestStreak", lambda r: (-(r["hitRate"] or 0), -r["total"])),
        "interviews": top("interviews", lambda r: (-(r["per100"] or 0), name(r))),
        "conversion": top("per100", lambda r: (-r["interviews"], name(r)),
                          [r for r in rows if r["total"] >= MIN_BIDS_FOR_RATE]),
    }


def day_windows(start: date, end: date, sched: BidSchedule) -> list[DayWindow]:
    """One window per local date of [start, end]: the daily window ending at that date's cutoff."""
    out = []
    for i in range((end - start).days + 1):
        cutoff = datetime.combine(start + timedelta(days=i), sched.daily_at, tzinfo=sched.tz)
        s, e = window_for("daily", cutoff, sched)
        out.append(DayWindow(period_key(cutoff), s, e))
    return out


async def day_counts(
    bidders: list[Bidder], start: date, end: date, sched: BidSchedule,
) -> dict[tuple[str, date], tuple[int, int, int]]:
    """{(bidder id, report day): (approved, pending, rejected)} over [start, end]."""
    windows = day_windows(start, end, sched)
    days = [date.fromisoformat(w.key) for w in windows]
    return {(b, days[i]): c for (b, i), c in (await _day_counts(bidders, windows)).items()}


async def last_bid_at(bidder_ids: list) -> dict[str, datetime]:
    """{bidder id: latest firstAt of any of its bids}, whatever the period."""
    if not bidder_ids:
        return {}
    pipeline = [{"$match": {"bidderId": {"$in": bidder_ids}}},
                {"$group": {"_id": "$bidderId", "at": {"$max": "$firstAt"}}}]
    return {str(g["_id"]): g["at"] for g in await Bid.get_motor_collection().aggregate(pipeline).to_list(None)}


async def interview_credit(profile_ids: list, bidder_ids: list, start: date, end: date) -> tuple[dict, dict]:
    """Rounds held in [start, end] (local days), credited by who applied: ({bidder id: rounds} of interviews tagged to
    one of `bidder_ids`, {profile id: rounds} of untagged interviews on one of `profile_ids`). Interviews tagged self
    are no bidder's. Untagged ones (from before "Applied by") go to every bidder on their profile."""
    tz = app_tz()
    first = local_day_start_utc(start, tz)
    last = local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    whose = [c for c in ({"accountId": {"$in": profile_ids}} if profile_ids else None,
                         {"appliedBy.bidderId": {"$in": bidder_ids}} if bidder_ids else None) if c]
    if not whose:
        return {}, {}
    ivs = await Interview.find({"$and": [
        {"$or": whose},
        {"$or": [{"scheduledAt": {"$gte": first, "$lte": last}}, {"stageHistory.scheduledAt": {"$gte": first, "$lte": last}}]},
    ]}).to_list()
    tagged: dict[str, int] = {}
    untagged: dict[str, int] = {}
    wanted_profiles = {str(p) for p in profile_ids}
    wanted_bidders = {str(b) for b in bidder_ids}
    for iv in ivs:
        n = len(split_rounds_by_day(iv, first, last, tz))
        if not n:
            continue
        if iv.appliedBy is None:
            if str(iv.accountId) in wanted_profiles:
                untagged[str(iv.accountId)] = untagged.get(str(iv.accountId), 0) + n
        elif iv.appliedBy.kind == "bidder" and str(iv.appliedBy.bidderId) in wanted_bidders:
            tagged[str(iv.appliedBy.bidderId)] = tagged.get(str(iv.appliedBy.bidderId), 0) + n
    return tagged, untagged


def _local_day(at: datetime, sched: BidSchedule) -> date:
    """The report-timezone date of a stored (naive UTC) moment."""
    return at.replace(tzinfo=timezone.utc).astimezone(sched.tz).date()


def _ratio(n, d, digits):
    return round(n / d, digits) if d else None


async def performance(
    bidders: list[Bidder], period: str, start: date, end: date, label: str, today: date,
    columns: Optional[list[dict]] = None,
) -> dict:
    """The /bids/performance response (spec section 3): per-bidder bids by week, streaks, interviews and the top cards."""
    sched = schedule_from_settings()
    prev_start, prev_end = previous_range(period, start, end)
    counts = await day_counts(bidders, min(prev_start, start), end, sched)
    weeks = columns if columns is not None else week_columns(start, end)
    week_of = {date.fromisoformat(w["start"]) + timedelta(days=i): n
               for n, w in enumerate(weeks) for i in range((date.fromisoformat(w["end"]) - date.fromisoformat(w["start"])).days + 1)}

    rec = records_start_utc(sched)
    rec_day = rec.astimezone(sched.tz).date() if rec else None
    ids = list({b.profileId for b in bidders if b.profileId})
    accounts = {a.id: a for a in await Account.find({"_id": {"$in": ids}}).to_list()} if ids else {}
    owner_ids = list({b.createdBy for b in bidders})
    owners = {u.id: u.name for u in await UserExtra.find({"_id": {"$in": owner_ids}}).to_list()} if owner_ids else {}
    tagged, untagged = await interview_credit(ids, [b.id for b in bidders], start, end)
    active = [b async for b in Bidder.find({"profileId": {"$in": ids}, "archivedAt": None})] if ids else []
    typed = await self_bids(list({a.createdBy for a in accounts.values()}), {str(i) for i in ids}, start, end) if ids else {}
    typed_on = {acc for (acc, _d), n in typed.items() if n}
    last = await last_bid_at([b.id for b in bidders])

    rows = []
    for b in bidders:
        k = str(b.id)
        mine = {d: sum(c[:2]) for (bid, d), c in counts.items() if bid == k}
        cur = [(d, c) for (bid, d), c in counts.items() if bid == k and start <= d <= end]
        weekly = [0] * len(weeks)
        for d, c in cur:
            if d in week_of:  # stats-only callers pass no columns
                weekly[week_of[d]] += c[0] + c[1]
        approved, pending, rejected = (sum(c[i] for _d, c in cur) for i in range(3))
        total = approved + pending
        prev_total = sum(n for d, n in mine.items() if prev_start <= d <= prev_end)
        first = max([start, _local_day(b.createdAt, sched)] + ([rec_day] if rec_day else []))
        end_day = min(end, _local_day(b.archivedAt, sched)) if b.archivedAt else end
        st = streaks(mine, first, end_day, today)
        from_profile = untagged.get(str(b.profileId), 0) if b.profileId else 0
        interviews = tagged.get(k, 0) + from_profile if b.profileId or k in tagged else None
        shared_with = sorted({o.name for o in active if b.profileId and o.profileId == b.profileId and o.id != b.id},
                             key=str.lower)
        profile_self_bids = bool(b.profileId) and str(b.profileId) in typed_on
        pay = round(approved * b.rate, 2)  # pending isn't paid yet
        reviewed = approved + rejected
        rows.append({
            "id": k, "name": b.name, "ownerName": owners.get(b.createdBy), "profileId": str(b.profileId) if b.profileId else None,
            "profileName": accounts[b.profileId].name if b.profileId in accounts else None,
            "status": "archived" if b.archivedAt else "active", "weekly": weekly, "total": total, "approved": approved,
            "pending": pending, "rejected": rejected, "previousTotal": prev_total,
            "change": _ratio(total - prev_total, prev_total, 3), **st,
            "hitRate": _ratio(st["hitDays"], st["weekdays"], 3), "avgPerWeekday": _ratio(total, st["weekdays"], 1),
            "interviews": interviews, "sharedProfile": bool(shared_with) or profile_self_bids,
            "sharedWith": shared_with, "profileSelfBids": profile_self_bids,
            "untaggedInterviews": from_profile,
            "days": day_strip(mine, start, end, first, end_day, today),
            "per100": _ratio(interviews * 100, total, 2) if interviews is not None and total >= MIN_BIDS_FOR_RATE else None,
            "lastBidAt": _iso_utc(last.get(k)),
            "rate": b.rate, "pay": pay, "reviewed": reviewed, "approvalRate": _ratio(approved, reviewed, 3),
            "costPerInterview": _ratio(pay, interviews, 2) if interviews else None,
        })
    rows.sort(key=lambda r: (-r["total"], r["name"].lower()))

    total = sum(r["total"] for r in rows)
    weekdays = sum(r["weekdays"] for r in rows)
    interviews = sum(tagged.values()) + sum(untagged.values())  # each round once: by its bidder, or by its profile
    pay = round(sum(r["pay"] for r in rows), 2)
    approved = sum(r["approved"] for r in rows)
    rejected = sum(r["rejected"] for r in rows)
    return {
        "period": period, "start": start.isoformat(), "end": end.isoformat(), "label": label,
        "recordsStart": rec_day.isoformat() if rec_day else None, "weeks": weeks, "bidders": rows,
        "totals": {
            "total": total, "interviews": interviews, "hitRate": _ratio(sum(r["hitDays"] for r in rows), weekdays, 3),
            "avgPerWeekday": _ratio(total, weekdays, 1),
            "per100": _ratio(interviews * 100, total, 2) if total >= MIN_BIDS_FOR_RATE else None,
            "pay": pay, "approved": approved, "rejected": rejected,
            "approvalRate": _ratio(approved, approved + rejected, 3),
            "costPerInterview": _ratio(pay, interviews, 2) if interviews else None,
        },
        "top": rank(rows),
    }
