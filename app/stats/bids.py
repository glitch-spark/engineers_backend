"""Statistics → Bids: the subject's bids against the team (see docs/superpowers/specs/2026-11-18-stats-insights-design.md)
and the rule-based tips. Pure rule functions return a tip dict (with an `impact` for ordering) or None."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean, median
from typing import Optional

from app import interview_rates
from app.stats import frame
from app.stats.profile_data import idle_profiles, profile_last_bids

SELF_SHARE_GAP = 0.15  # bad when the self share is this far below the team median share
WEAK_DAY_RATIO = 0.6
BEHIND_RATIO = 0.85
_DAYS = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays"]

DEFINITIONS = [
    "Self bids are the applies you typed in your daily plans, on every saved day (drafts included).",
    "Bidder bids are your bidders' Bid Track approved + pending bids, counted on the Bid Track day.",
    "Bidder bids are Bid Track counts on every day; before Nov 16, 2026 the Plans page showed typed bidder numbers, so older days can differ.",
]


def _tip(id: str, severity: str, text: str, detail: str, evidence: dict, impact: float) -> dict:
    return frame.tip(f"bids.{id}", severity, text, detail, evidence, impact)


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
    names = " and ".join(_DAYS[d] for d in sorted(weak))
    return _tip(
        "weak_weekdays", "watch",
        f"{names} are your low days: {mean(low):.0f} bids avg vs {mean(rest):.0f} on other days.",
        "Spreading bids evenly across the week keeps your profiles active every day.",
        {"weekdays": sorted(weak), "lowAvg": mean(low), "otherAvg": mean(rest)}, (mean(rest) - mean(low)) * len(weak))


def behind_target(actual: int, target: int) -> Optional[dict]:
    if not target or actual >= BEHIND_RATIO * target:
        return None
    return _tip(
        "behind_target", "bad",
        f"You are at {actual} bids against a target of {target} so far ({actual / target:.0%}).",
        f"{target - actual} bids short of your plan's daily targets for the days elapsed.",
        {"actual": actual, "target": target}, target - actual)


def up_vs_last_period(now: int, before: int, window: str = "the previous period") -> Optional[dict]:
    return frame.up_vs_last_period("bids", "bids", now, before, window)


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


async def team_counts(subject_id, start: date, end: date, region: str, days: Optional[dict] = None) -> tuple:
    """The Bids frame's people: (non-admin users by id, their ids + the subject's, profile regions,
    interview_rates.counts over [start, end] on every stored day)."""
    from app.models import UserExtra

    users = {u.id: u for u in await UserExtra.find({"role": {"$ne": "admin"}}).to_list()}
    user_ids = list(users) + ([] if subject_id in users else [subject_id])
    regions, _ = await interview_rates._profile_regions()
    now = await interview_rates.counts(user_ids, start, end, region, regions, days, logged_only=False)
    return users, user_ids, regions, now


async def bids_view(subject, period: str, day: date, region: str, until: Optional[date], today: date) -> dict:
    from app.routers.metrics import _display_name

    start, end = interview_rates.window(period, day, until)
    days: dict = {}
    users, user_ids, regions, now = await team_counts(subject.id, start, end, region, days)

    team_ids = [uid for uid in users if now.get(uid, (0,))[0] > 0]
    in_team = subject.id in team_ids
    bids = {uid: now[uid][0] if uid in now else 0 for uid in user_ids}
    bidder = {uid: now[uid][2] if uid in now else 0 for uid in user_ids}
    totals = [bids[u] for u in team_ids]
    wdays = frame.working_days(start, end, today)
    n_days = len(wdays)
    me = bids[subject.id]

    # Today is still in progress: pace (per day, weak weekdays, behind target) looks at the working days before it.
    done_days = [d for d in wdays if d < today]

    def per_day_of(uid) -> float:
        if not done_days:
            return bids[uid] / n_days if n_days else 0
        return sum(sum(days.get((d, uid), (0, 0))[:2]) for d in done_days) / len(done_days)

    dist, per_day = frame.distribution(totals), frame.distribution([per_day_of(u) for u in team_ids])
    rank, of = frame.rank_of(me, totals, True, in_team)
    my_per_day = per_day_of(subject.id)
    # Bidder targets have no region split, so a region view has no target to compare with.
    target = await _targets(subject.id, done_days) if done_days and region == "all" else 0
    note = f"team {round(per_day['median'] or 0)}" + (f" · target {round(target / len(done_days))}" if target else "")
    headline = [
        {"key": "bids", "label": "Your bids", "value": me, "format": "count", "team": dist, "rank": rank, "of": of,
         "vsMedian": frame.vs_median(me, dist["median"]), "higherIsBetter": True,
         "note": f"Self {me - bidder[subject.id]} · bidder {bidder[subject.id]}"},
        {"key": "rank", "label": "Your rank", "value": rank, "format": "rank", "team": None, "rank": rank, "of": of,
         "vsMedian": None, "higherIsBetter": False, "note": f"of {of}"},
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

    trend = frame.trend(start, end, today, days, (0, 1), subject.id, team_ids)

    not_enough = frame.not_enough_data(len(team_ids), n_days)
    tips: list[dict] = []
    if not not_enough:
        shares = [(bids[u] - bidder[u]) / bids[u] for u in team_ids]
        def mine(d: date) -> int:
            v = days.get((d, subject.id), (0, 0))
            return v[0] + v[1]

        per_weekday: dict = defaultdict(list)
        for d in done_days:
            per_weekday[d.weekday()].append(mine(d))
        candidates = [low_self_share(me - bidder[subject.id], me, shares), weak_weekdays(per_weekday)]
        if me:
            last, names = await profile_last_bids(subject.id, region, today)
            candidates.append(idle_profiles(last, today, names))
        if target:
            candidates.append(behind_target(sum(mine(d) for d in done_days), target))
        ps, pe = frame.previous_window(period, start, end, today)
        before = (await interview_rates.counts([subject.id], ps, pe, region, regions, logged_only=False)).get(subject.id, (0,))[0]
        candidates.append(up_vs_last_period(me, before, frame.span_label(ps, pe)))
        if not me:
            candidates.append(_tip("no_bids", "bad", "No bids yet this period.",
                                   f"The team median is {round(dist['median'] or 0)} bids so far.", {"teamMedian": dist["median"]}, dist["median"] or 0))
        tips = frame.order_tips([t for t in candidates if t])

    return {
        "tab": "bids", "label": interview_rates.label(period, start, end),
        "subject": {"userId": str(subject.id), "name": _display_name(subject)},
        "headline": headline, "tips": tips, "everyone": {"rowKind": "person", "metric": "bids", "rows": rows},
        "trend": trend, "notEnoughData": not_enough, "definitions": DEFINITIONS,
    }
