"""Pending items → Bidders (spec 2026-11-21-admin-home-design.md): an owner's active bidders (not archived,
registered) that stopped bidding or slowed down. Working days are Mon-Fri and today never counts; days are Bid Track
report days, counted as the Daily Plan counts bidder bids."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from app import plan_bidder
from app.models.bidder import Bidder

IDLE_DAYS = 2  # working days without a bid
LOOKBACK = timedelta(days=14)  # a bid this recently, or the bidder is not "not bidding" (and no newer bidder slows)
RECENT, BEFORE = 3, 10  # working days compared, and the ones before them
MIN_BEFORE = 10  # bids a day before, under which a drop says nothing


def bidder_flag(daily: dict[date, int], today: date, started: date) -> Optional[dict]:
    """{kind, days, recent, before} for one bidder's bids per day, or None. `not_bidding`: a bid in the 14 days
    before `today` and none since on at least 2 working days (`days`: how many). Else `slowing`: the last 3 working
    days (`recent`, oldest first) average under half of the 10 before them (`before`, a day), that average at least
    10; never for a bidder who `started` (created / registered) under 14 days ago."""
    work = [d for d in (today - timedelta(days=n) for n in range(1, 22)) if d.weekday() < 5][:RECENT + BEFORE]
    last = max((d for d, n in daily.items() if n and d < today), default=None)
    if last is not None and last >= today - LOOKBACK:
        idle = sum(1 for d in work if d > last)
        if idle >= IDLE_DAYS:
            return {"kind": "not_bidding", "days": idle, "recent": None, "before": None}
    if started > today - LOOKBACK:
        return None
    recent = [daily.get(d, 0) for d in reversed(work[:RECENT])]
    before = sum(daily.get(d, 0) for d in work[RECENT:]) / BEFORE
    if before >= MIN_BEFORE and sum(recent) / RECENT < before / 2:
        return {"kind": "slowing", "days": None, "recent": recent, "before": round(before, 1)}
    return None


async def bidder_alerts(owner_ids: list, today: date) -> dict:
    """{owner id: [{bidderId, name, kind, days, recent, before}] by name} for the owners with a flagged bidder."""
    bidders = await Bidder.find(
        {"createdBy": {"$in": owner_ids}, "archivedAt": None, "registeredAt": {"$ne": None}}).to_list()
    counts = await plan_bidder.bids_by_day([b.id for b in bidders], today - timedelta(days=21), today - timedelta(days=1))
    out: dict = {}
    for b in sorted(bidders, key=lambda b: b.name.lower()):
        daily = {day: n for (bidder_id, day), n in counts.items() if bidder_id == b.id}
        flag = bidder_flag(daily, today, max(b.createdAt, b.registeredAt).date())
        if flag:
            out.setdefault(b.createdBy, []).append({"bidderId": str(b.id), "name": b.name, **flag})
    return out
