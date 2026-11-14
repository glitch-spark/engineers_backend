"""Bidders page insights (spec 2026-11-14-bidders-insights-design.md): one owner's active bidders pooled vs every
owner's (the platform) over the Performance tab's period, and each measure's best bidder vs the best on the platform.
Numbers come from `performance()`, so they match the Performance table; the platform side is cached per window."""
from __future__ import annotations

from datetime import date
from typing import Optional

from app.bids.performance import MIN_BIDS_FOR_RATE, performance
from app.models.bidder import Bidder
from app.profile_compare import BenchmarkCache

MIN_COST_INTERVIEWS = 3  # fewer interviews than this: a cost per interview is too noisy to rank


def _ratio(n, d, digits):
    return round(n / d, digits) if d else None


def bidder_stats(perf: dict) -> Optional[dict]:
    """A group of bidders pooled: volumes over the bidders with bids (idle ones dropped), interviews once per round
    (the performance totals), pay = approved x rate. None when no bidder has bids."""
    rows = [r for r in perf["bidders"] if r["status"] == "active" and r["total"] > 0]
    if not rows:
        return None
    bids = sum(r["total"] for r in rows)
    weekdays = sum(r["weekdays"] for r in rows)
    hit_days = sum(r["hitDays"] for r in rows)
    approved = sum(r["approved"] for r in rows)
    rejected = sum(r["rejected"] for r in rows)
    interviews = perf["totals"]["interviews"]
    pay = round(sum(r["pay"] for r in rows), 2)
    return {
        "bidders": len(rows), "bids": bids, "weekdays": weekdays, "hitDays": hit_days,
        "hitRate": _ratio(hit_days, weekdays, 3), "avgPerWeekday": _ratio(bids, weekdays, 1),
        "approved": approved, "rejected": rejected, "reviewed": approved + rejected,
        "approvalRate": _ratio(approved, approved + rejected, 3), "interviews": interviews,
        "per100": _ratio(interviews * 100, bids, 2) if bids >= MIN_BIDS_FOR_RATE else None,
        "pay": pay, "costPerInterview": _ratio(pay, interviews, 2) if interviews else None,
    }


def _best(rows: list[dict], value, *, lowest: bool = False) -> Optional[dict]:
    ranked = [(value(r), r) for r in rows]
    ranked = [(v, r) for v, r in ranked if v is not None]
    if not ranked:
        return None
    sign = 1 if lowest else -1
    v, r = min(ranked, key=lambda x: (sign * x[0], -x[1]["total"], x[1]["name"].lower()))
    return {"id": r["id"], "name": r["name"], "ownerName": r.get("ownerName") or "", "value": v}


def bidder_tops(rows: list[dict]) -> dict:
    """Each measure's best active bidder: most bids; best interviews per 100 bids (100+ bids); lowest cost per interview
    (3+ interviews). Ties: more bids, then name."""
    rows = [r for r in rows if r["status"] == "active"]
    return {
        "bids": _best(rows, lambda r: r["total"] or None),
        "per100": _best(rows, lambda r: r["per100"]),
        "costPerInterview": _best(
            rows, lambda r: r["costPerInterview"] if (r["interviews"] or 0) >= MIN_COST_INTERVIEWS else None, lowest=True),
    }


async def _platform(key: tuple) -> dict:
    period, start, end, today = key
    everyone = await Bidder.find({"archivedAt": None}).to_list()
    perf = await performance(everyone, period, start, end, "", today)
    return {"stats": bidder_stats(perf), "tops": bidder_tops(perf["bidders"])}


# Every owner's bidders over one window: loads all their bids, so remembered (stale-while-revalidate, one at a time).
platform_cache = BenchmarkCache(_platform)


async def insights(bidders: list[Bidder], period: str, start: date, end: date, label: str, today: date) -> dict:
    """The /bids/insights response for one owner's bidders (archived ones never count)."""
    active = [b for b in bidders if b.archivedAt is None]
    mine_perf = await performance(active, period, start, end, label, today)
    plat = await platform_cache.get((period, start, end, today))
    mine_tops = bidder_tops(mine_perf["bidders"])

    def own(t):
        return {k: t[k] for k in ("id", "name", "value")} if t else None

    def public(t):  # another owner's bidder: names only, never its id
        return {k: t[k] for k in ("name", "ownerName", "value")} if t else None

    return {
        "start": start.isoformat(), "end": end.isoformat(), "label": label,
        "mine": bidder_stats(mine_perf), "platform": plat["stats"],
        "tops": {m: {"mine": own(mine_tops[m]), "platform": public(plat["tops"][m])} for m in mine_tops},
    }
