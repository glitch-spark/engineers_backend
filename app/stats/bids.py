"""Statistics → Bids: the subject's bids against the team (see docs/superpowers/specs/2026-11-18-stats-insights-design.md)
and the rule-based tips. Pure rule functions return a tip dict (with an `impact` for ordering) or None."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean, median
from typing import Optional

from app import interview_rates
from app.stats import frame

SELF_SHARE_GAP = 0.15  # bad when the self share is this far below the team median share
WEAK_DAY_RATIO = 0.6
IDLE_DAYS = 7
BEHIND_RATIO = 0.85
UP_RATIO = 0.15
_DAYS = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays"]

DEFINITIONS = [
    "Self bids are the applies you typed in your daily plans, on every saved day (drafts included).",
    "Bidder bids are your bidders' Bid Track approved + pending bids, counted on the Bid Track day.",
]


def _tip(id: str, severity: str, text: str, detail: str, evidence: dict, impact: float) -> dict:
    return {"id": f"bids.{id}", "severity": severity, "text": text, "detail": detail, "evidence": evidence,
            "impact": impact}


def low_self_share(me_self: int, me_total: int, team_shares: list[float]) -> Optional[dict]:
    if not me_total or not team_shares:
        return None
    share, team = me_self / me_total, median(team_shares)
    if share >= team - SELF_SHARE_GAP:
        return None
    missing = round((team - share) * me_total)
    return _tip(
        "low_self_share", "bad",
        f"Your self bids are {share:.0%} of your total vs {team:.0%} for the team.",
        f"Self {me_self} of {me_total}. Raising self applies by about {missing} closes the gap.",
        {"selfShare": share, "teamMedianShare": team, "self": me_self, "total": me_total}, missing)


def weak_weekdays(per_weekday: dict[int, list[int]]) -> Optional[dict]:
    """Weekdays (0 = Monday) whose average is under WEAK_DAY_RATIO of the other weekdays', each with ≥ 2 samples."""
    avgs = {d: mean(v) for d, v in per_weekday.items() if len(v) >= 2}
    weak = [d for d, a in avgs.items()
            if (others := [x for k, v in per_weekday.items() if k != d for x in v]) and mean(others)
            and a < WEAK_DAY_RATIO * mean(others)]
    if not weak:
        return None
    low = [x for d in weak for x in per_weekday[d]]
    rest = [x for d, v in per_weekday.items() if d not in weak for x in v]
    if not rest:
        return None
    names = " and ".join(_DAYS[d] for d in sorted(weak))
    return _tip(
        "weak_weekdays", "watch",
        f"{names} are your low days: {mean(low):.0f} bids avg vs {mean(rest):.0f} on other days.",
        "Spreading bids evenly across the week keeps your profiles active every day.",
        {"weekdays": sorted(weak), "lowAvg": mean(low), "otherAvg": mean(rest)}, (mean(rest) - mean(low)) * len(weak))


def idle_profiles(profile_last_bid: dict[str, Optional[date]], today: date) -> Optional[dict]:
    idle = sorted(n for n, last in profile_last_bid.items() if last is None or (today - last).days >= IDLE_DAYS)
    if not idle:
        return None
    shown = ", ".join(idle[:3]) + (f" and {len(idle) - 3} more" if len(idle) > 3 else "")
    return _tip(
        "idle_profiles", "watch",
        f"{len(idle)} profile{'s' if len(idle) > 1 else ''} had no bids in the last {IDLE_DAYS} days: {shown}.",
        "Idle profiles lose search ranking on most job boards.",
        {"profiles": idle}, len(idle) * 10)  # rough: ~10 bids a profile a day-ish


def behind_target(actual: int, target: int) -> Optional[dict]:
    if not target or actual >= BEHIND_RATIO * target:
        return None
    return _tip(
        "behind_target", "bad",
        f"You are at {actual} bids against a target of {target} so far ({actual / target:.0%}).",
        f"{target - actual} bids short of your plan's daily targets for the days elapsed.",
        {"actual": actual, "target": target}, target - actual)


def up_vs_last_period(now: int, before: int) -> Optional[dict]:
    if not before or now / before - 1 < UP_RATIO:
        return None
    change = now / before - 1
    return _tip(
        "up_vs_last_period", "good", f"Your bids are up {change:+.0%} vs the previous period — keep it.",
        f"{now} bids vs {before} over the same number of days before.",
        {"now": now, "before": before, "change": change}, now - before)


async def _targets(uid, days: list[date]) -> int:
    """Σ day_targets (self + bidder bids) over `days`."""
    from app import plan_auto
    from app.models import DailyPlan

    weekly: dict = {}
    for monday in {d - timedelta(days=d.weekday()) for d in days}:
        weekly[monday] = (await plan_auto.weekly_plans_for([uid], monday)).get(uid)
    plans = {p.date.date(): p for p in await DailyPlan.find({
        "userId": uid, "date": {"$gte": datetime.combine(min(days), datetime.min.time()),
                                "$lte": datetime.combine(max(days), datetime.min.time())}}).to_list()}
    total = 0
    for d in days:
        t = plan_auto.day_targets(plans.get(d), weekly[d - timedelta(days=d.weekday())], d)
        total += t.bidsSelf + t.bidsBidder
    return total


async def _profile_last_bids(uid, region: str, today: date) -> dict[str, Optional[date]]:
    """Non-archived profiles of `uid` in the region → last day with a bid over the IDLE_DAYS ending `today`."""
    from app.models import DailyPlan
    from app.plan_profiles import owner_profiles

    profiles = [p for p in await owner_profiles(uid)
                if not p["archived"] and (region == "all" or p["region"] == region)]
    if not profiles:
        return {}
    first = today - timedelta(days=IDLE_DAYS - 1)
    last: dict = {}
    for plan in await DailyPlan.find({"userId": uid, "date": {
            "$gte": datetime.combine(first, datetime.min.time()), "$lte": datetime.combine(today, datetime.min.time())}}).to_list():
        for pid, n in plan.done.bidsSelfByProfile.items():
            if n:
                last[pid] = max(last.get(pid, plan.date.date()), plan.date.date())
    by_profile: dict = {}
    await interview_rates.bidder_bids([uid], first, today, "all", {}, by_profile)
    for (pid, day), n in by_profile.items():
        if n:
            last[pid] = max(last.get(pid, day), day)
    return {p["name"]: last.get(p["id"]) for p in profiles}


def _dist(values: list[float]) -> dict:
    return frame.distribution(values)


async def bids_view(subject, period: str, day: date, region: str, until: Optional[date], today: date) -> dict:
    from app.models import UserExtra
    from app.routers.metrics import _display_name

    start, end = interview_rates.window(period, day, until)
    users = {u.id: u for u in await UserExtra.find({"role": {"$ne": "admin"}}).to_list()}
    user_ids = list(users) + ([] if subject.id in users else [subject.id])
    regions, _ = await interview_rates._profile_regions()
    days: dict = {}
    now = await interview_rates.counts(user_ids, start, end, region, regions, days, logged_only=False)

    team_ids = [uid for uid in users if now.get(uid, (0,))[0] > 0]
    in_team = subject.id in team_ids
    bids = {uid: now[uid][0] if uid in now else 0 for uid in user_ids}
    bidder = {uid: now[uid][2] if uid in now else 0 for uid in user_ids}
    totals = [bids[u] for u in team_ids]
    wdays = frame.working_days(start, end, today)
    n_days = len(wdays)
    me = bids[subject.id]

    dist, per_day = _dist(totals), _dist([b / n_days for b in totals] if n_days else [])
    rank, of = frame.rank_of(me, totals, True, in_team)
    my_per_day = me / n_days if n_days else 0
    target = await _targets(subject.id, wdays) if wdays else 0
    note = f"team {round(per_day['median'] or 0)}" + (f" · target {round(target / n_days)}" if target else "")
    headline = [
        {"key": "bids", "label": "Your bids", "value": me, "format": "count", "team": dist, "rank": rank, "of": of,
         "vsMedian": frame.vs_median(me, dist["median"]), "higherIsBetter": True,
         "note": f"Self {me - bidder[subject.id]} · bidder {bidder[subject.id]}"},
        {"key": "rank", "label": "Your rank", "value": rank, "format": "rank", "team": dist, "rank": rank, "of": of,
         "vsMedian": frame.vs_median(me, dist["median"]), "higherIsBetter": False, "note": f"of {of}"},
        {"key": "perDay", "label": "Per working day", "value": round(my_per_day), "format": "count", "team": per_day,
         "rank": rank, "of": of, "vsMedian": frame.vs_median(my_per_day, per_day["median"]), "higherIsBetter": True,
         "note": note},
    ]

    shown = team_ids + ([] if in_team else [subject.id])
    rows = []
    for uid in shown:
        r, _ = frame.rank_of(bids[uid], totals, True, uid in team_ids)
        u = users.get(uid, subject)
        rows.append({"userId": str(uid), "name": _display_name(u), "value": bids[uid],
                     "vsMedian": frame.vs_median(bids[uid], dist["median"]), "rank": r,
                     "parts": {"self": bids[uid] - bidder[uid], "bidder": bidder[uid]}, "isSubject": uid == subject.id})
    rows.sort(key=lambda r: (-r["value"], r["name"].lower()))

    unit, buckets = frame.trend_buckets(start, end)
    trend = []
    for s, e, lbl in buckets:
        sums: dict = defaultdict(int)
        for (d, uid), v in days.items():
            if s <= d <= e:
                sums[uid] += v[0] + v[1]
        trend.append({"label": lbl, "you": sums[subject.id],
                      "median": median([sums[u] for u in team_ids]) if team_ids else 0})

    not_enough = len(team_ids) < 3 or n_days < 3
    tips: list[dict] = []
    if not not_enough:
        shares = [(bids[u] - bidder[u]) / bids[u] for u in team_ids]
        per_weekday: dict = defaultdict(list)
        for d in wdays:
            v = days.get((d, subject.id), (0, 0))
            per_weekday[d.weekday()].append(v[0] + v[1])
        candidates = [low_self_share(me - bidder[subject.id], me, shares), weak_weekdays(per_weekday)]
        if me:
            candidates.append(idle_profiles(await _profile_last_bids(subject.id, region, today), today))
        if target:
            candidates.append(behind_target(sum(days.get((d, subject.id), (0, 0))[0] + days.get((d, subject.id), (0, 0))[1]
                                                for d in wdays), target))
        ps, pe = frame.previous_window(period, start, end, today)
        before = (await interview_rates.counts([subject.id], ps, pe, region, regions, logged_only=False)).get(subject.id, (0,))[0]
        candidates.append(up_vs_last_period(me, before))
        tips = frame.order_tips([t for t in candidates if t])

    return {
        "tab": "bids", "label": interview_rates.label(period, start, end),
        "subject": {"userId": str(subject.id), "name": _display_name(subject)},
        "headline": headline, "tips": tips, "everyone": {"metric": "bids", "rows": rows},
        "trend": {"unit": unit, "buckets": trend}, "notEnoughData": not_enough, "definitions": DEFINITIONS,
    }
