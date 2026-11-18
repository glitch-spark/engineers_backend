"""Statistics → Profiles: the subject's profiles against the team (see docs/superpowers/specs/2026-11-18-stats-insights-design.md)
and the rule-based tips. Pure rule functions return a tip dict (with an `impact` for ordering) or None."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from statistics import median
from typing import Optional

from app import interview_rates
from app.profile_metrics import since_date
from app.stats import frame
from app.stats.profile_data import idle_profiles, profile_last_bids

MIN_BIDS = interview_rates.MIN_BIDS
DEAD_BIDS = 150
DEAD_DAYS = 28
REGION_GAP = 2.0

DEFINITIONS = [
    "Profile bids are the self bids typed for the profile (every saved day) plus its bidders' Bid Track approved + pending bids.",
    "Profile interviews are the rounds held in the period on the profile's interviews (same rules as the Interviews tab).",
    f"Interviews per 100 bids are shown and ranked only for profiles with at least {MIN_BIDS} bids; an active profile is one with a bid in the period.",
]
SINCE_NOTE = "Profile numbers start on {date}, when bids began to be typed per profile."


def _tip(id: str, severity: str, text: str, detail: str, evidence: dict, impact: float) -> dict:
    return frame.tip(f"profiles.{id}", severity, text, detail, evidence, impact)


def dead_profile(rows_28d: dict) -> Optional[dict]:
    """`rows_28d`: profile id → profile_counts row over the 28 days ending yesterday. Non-archived profiles with
    ≥ DEAD_BIDS bids and no interviews."""
    dead = sorted(((pid, r) for pid, r in rows_28d.items()
                   if not r["archived"] and r["bids"] >= DEAD_BIDS and not r["interviews"]),
                  key=lambda x: (-x[1]["bids"], x[1]["name"].lower(), x[0]))
    if not dead:
        return None
    shown = ", ".join(r["name"] for _, r in dead[:3]) + (f" and {len(dead) - 3} more" if len(dead) > 3 else "")
    total = sum(r["bids"] for _, r in dead)
    return _tip(
        "dead_profile", "bad",
        f"{shown}: no interviews from {total} bids in the last {DEAD_DAYS} days.",
        "A profile with this many bids and no interview needs a rewrite or a rest.",
        {"profileIds": [pid for pid, _ in dead], "profiles": [r["name"] for _, r in dead], "bids": total}, total)


def region_gap(by_region: dict[str, tuple[int, int]]) -> Optional[dict]:
    """`by_region`: region → (bids, interviews). Regions with ≥ MIN_BIDS bids; best per-100 ≥ 2× the worst and > 0."""
    rates = {r: i / b * 100 for r, (b, i) in by_region.items() if b >= MIN_BIDS}
    if len(rates) < 2:
        return None
    best, worst = max(rates, key=rates.get), min(rates, key=rates.get)
    if not rates[best] or rates[best] < REGION_GAP * rates[worst]:
        return None
    ratio = f"{rates[best] / rates[worst]:.1f}×" if rates[worst] else "far more than"
    return _tip(
        "region_gap", "watch",
        f"{best} profiles convert {ratio} {worst} ({rates[best]:.1f} vs {rates[worst]:.1f} per 100).",
        f"{by_region[worst][0]} bids on {worst} profiles gave {by_region[worst][1]} interviews; consider shifting bids to {best}.",
        {"best": best, "worst": worst, "bestPer100": rates[best], "worstPer100": rates[worst]},
        (rates[best] - rates[worst]) / 100 * by_region[worst][0])


async def profile_counts(uid, start: date, end: date, region: str) -> dict[str, dict]:
    """The owner's profiles in the region (archived included) → name, region, archived, bids (self + bidder), self,
    bidder and interviews (rounds held) over the local days [start, end]."""
    from app.dashboard_stats import app_tz, local_day_start_utc
    from app.interview_rounds import held_round_results, load_interviews_for_window
    from app.models import DailyPlan
    from app.plan_profiles import owner_profiles

    out = {p["id"]: {"name": p["name"], "region": p["region"], "archived": p["archived"],
                     "bids": 0, "selfBids": 0, "bidderBids": 0, "interviews": 0}
           for p in await owner_profiles(uid) if region == "all" or p["region"] == region}
    if not out:
        return out
    for plan in await DailyPlan.find({"userId": uid, "date": {
            "$gte": datetime.combine(start, datetime.min.time()), "$lte": datetime.combine(end, datetime.min.time())}}).to_list():
        for pid, n in plan.done.bidsSelfByProfile.items():
            if pid in out:
                out[pid]["selfBids"] += n
    by_profile: dict = {}
    await interview_rates.bidder_bids([uid], start, end, "all", {}, by_profile)
    for (pid, _day), n in by_profile.items():
        if pid in out:
            out[pid]["bidderBids"] += n
    tz = app_tz()
    first, last = local_day_start_utc(start, tz), local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    for iv in await load_interviews_for_window([uid], first, last):
        if str(iv.accountId) in out:
            out[str(iv.accountId)]["interviews"] += len(held_round_results(iv, first, last, tz))
    for r in out.values():
        r["bids"] = r["selfBids"] + r["bidderBids"]
    return out


def _per100(r: dict) -> Optional[float]:
    return round(r["interviews"] / r["bids"] * 100, 1) if r["bids"] >= MIN_BIDS else None


def _active(rows: dict) -> list[dict]:
    return [r for r in rows.values() if not r["archived"] and r["bids"] > 0]


async def profiles_view(subject, period: str, day: date, region: str, until: Optional[date], today: date) -> dict:
    from app.models import UserExtra
    from app.routers.metrics import _display_name

    start, end = interview_rates.window(period, day, until)
    since = await since_date()  # per-profile self bids exist only from here
    blind = since is None or since > end
    if not blind:
        start = max(start, since)
    users = {u.id: u for u in await UserExtra.find({"role": {"$ne": "admin"}}).to_list()}
    user_ids = list(users) + ([] if subject.id in users else [subject.id])
    # ponytail: three queries per user, run concurrently; batch across users if the team grows past a few hundred.
    # A period entirely before `since` counts an empty window (end + 1 .. end) so nothing is shown.
    cstart = end + timedelta(days=1) if blind else start
    counted = await asyncio.gather(*(profile_counts(uid, cstart, end, region) for uid in user_ids))
    data = dict(zip(user_ids, counted))
    sid = subject.id
    team_ids = [uid for uid in users if _active(data[uid])]
    in_team = sid in team_ids

    def rates(uid) -> list[float]:
        return [x for r in _active(data[uid]) if (x := _per100(r)) is not None]

    def best(uid) -> Optional[float]:
        return max(rates(uid), default=None)

    def worst(uid) -> Optional[float]:
        return min(rates(uid), default=None)

    def zero(uid) -> int:
        return sum(1 for r in _active(data[uid]) if r["bids"] >= MIN_BIDS and not r["interviews"])

    bests = {u: best(u) for u in team_ids}
    rated = [v for v in bests.values() if v is not None]
    best_d = frame.distribution(rated)
    worsts = [v for u in team_ids if (v := worst(u)) is not None]
    worst_d = frame.distribution(worsts)
    zero_d = frame.distribution([zero(u) for u in team_ids])
    mine, my_best, my_worst = data[sid], best(sid), worst(sid)
    my_active, my_zero = _active(mine), zero(sid)

    def name_of(v):
        return next((r["name"] for r in my_active if v is not None and _per100(r) == v), "")

    rank, of = frame.rank_of(my_best, rated, True, in_team) if my_best is not None else (None, len(rated))
    w_rank, w_of = frame.rank_of(my_worst, worsts, True, in_team) if my_worst is not None else (None, len(worsts))
    z_rank, z_of = frame.rank_of(my_zero, [zero(u) for u in team_ids], False, in_team)
    zm = zero_d["median"]
    headline = [
        {"key": "best", "label": "Best profile", "value": my_best, "format": "per100", "team": best_d, "rank": rank, "of": of,
         "vsMedian": frame.vs_median(my_best, best_d["median"]) if my_best is not None else None, "higherIsBetter": True,
         "note": name_of(my_best)},
        {"key": "rank", "label": "Your rank", "value": rank, "format": "rank", "team": None, "rank": rank, "of": of,
         "vsMedian": None, "higherIsBetter": False,
         "note": f"of {of}" if my_best is not None else f"needs a profile with {MIN_BIDS} bids"},
        {"key": "worst", "label": "Weakest profile", "value": my_worst, "format": "per100", "team": worst_d,
         "rank": w_rank, "of": w_of,
         "vsMedian": frame.vs_median(my_worst, worst_d["median"]) if my_worst is not None else None,
         "higherIsBetter": True, "note": name_of(my_worst)},
        {"key": "zero", "label": "Profiles with 0 interviews", "value": my_zero, "format": "count", "team": zero_d,
         "rank": z_rank, "of": z_of, "vsMedian": frame.vs_median(my_zero, zm), "higherIsBetter": False,
         "note": f"of {len(my_active)} active · team {'–' if zm is None else format(zm, 'g')}"},
    ]

    # Archived profiles are listed (when they had bids) but not ranked.
    ranked = [x for r in mine.values() if not r["archived"] and (x := _per100(r)) is not None]
    mid = median(ranked) if len(ranked) >= 2 else None
    rows = []
    for pid, r in mine.items():
        if not (r["bids"] or r["interviews"]):
            continue
        v = _per100(r)
        rows.append({"userId": pid, "name": r["name"], "region": r["region"], "value": v,
                     "vsMedian": frame.vs_median(v, mid) if v is not None and mid is not None else None,
                     "rank": frame.rank_of(v, ranked)[0] if v is not None and not r["archived"] else None,
                     "parts": {"self": r["selfBids"], "bidder": r["bidderBids"]}, "isSubject": False})
    rows.sort(key=lambda r: (r["rank"] is None, -(r["value"] or 0), -(r["parts"]["self"] + r["parts"]["bidder"]), r["name"].lower()))

    not_enough = blind or frame.not_enough_data(len(team_ids), len(frame.working_days(start, end, today)))
    tips: list[dict] = []
    if not not_enough:
        by_region: dict = {}
        for r in my_active:
            if r["region"]:
                b, i = by_region.get(r["region"], (0, 0))
                by_region[r["region"]] = (b + r["bids"], i + r["interviews"])
        dead = await profile_counts(sid, max(today - timedelta(days=DEAD_DAYS), since), today - timedelta(days=1), region)
        last, names = await profile_last_bids(sid, region, today)
        candidates = [dead_profile(dead), region_gap(by_region), idle_profiles(last, today, names)]
        if not my_active:
            n = sum(1 for r in mine.values() if not r["archived"])
            candidates.append(_tip("no_profiles", "bad", f"None of your {n} profiles had a bid this period." if n else "No active profiles this period.",
                                   "Bid on them, or archive ones you no longer use." if n else "Bids need a profile: add one or reactivate an archived one.", {}, 0))
        tips = frame.order_tips([t for t in candidates if t])

    return {
        "tab": "profiles", "label": interview_rates.label(period, start, end),
        "subject": {"userId": str(sid), "name": _display_name(subject)},
        "headline": headline, "tips": tips, "everyone": {"rowKind": "profile", "metric": "per100", "rows": rows},
        "trend": None, "notEnoughData": not_enough, "definitions": DEFINITIONS + ([SINCE_NOTE.format(date=f"{since:%b} {since.day}, {since.year}")] if since else []),
    }
