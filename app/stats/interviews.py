"""Statistics → Interviews: the subject's interviews against the team (see docs/superpowers/specs/2026-11-18-stats-insights-design.md)
and the rule-based tips. Pure rule functions return a tip dict (with an `impact` for ordering) or None."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Optional

from app import interview_rates
from app.stats import frame

MIN_BIDS = interview_rates.MIN_BIDS
LOW_CONVERSION = 0.6  # bad when per-100 is under this share of the team median
STALL_RATIO = 0.6  # watch when a group's pass rate is under this share of the team's
MIN_DECIDED = 4  # passed + rejected rounds needed on each side to trust a pass rate
CALLER_GAP = 0.15
TECH_OR_LATER = frozenset({"tech", "hiring", "panel", "final"})
_GROUP_LABEL = {"intro": "Intro rounds", "tech": "Tech rounds", "hiring": "Hiring manager rounds",
                "panel": "Panel rounds", "final": "Final rounds"}

DEFINITIONS = [
    "An interview is a round held in the period (canceled rounds, AI interviews and home assessments are not counted), in the profile's region under a region filter.",
    "Interviews per 100 bids use the Bids tab's bids; it is ranked only for people with at least 30 bids.",
    "Pass rate is passed ÷ (passed + rejected) over rounds held in the period; rounds still awaiting a result are left out.",
]


def _tip(id: str, severity: str, text: str, detail: str, evidence: dict, impact: float) -> dict:
    return frame.tip(f"interviews.{id}", severity, text, detail, evidence, impact)


def _rate(passed: int, rejected: int) -> Optional[float]:
    return passed / (passed + rejected) if passed + rejected else None


def low_conversion(my_rate: Optional[float], team_median_rate: Optional[float], my_bids: int) -> Optional[dict]:
    """Rates are interviews per bid."""
    if my_bids < MIN_BIDS or not my_rate or not team_median_rate or my_rate >= LOW_CONVERSION * team_median_rate:
        return None
    missing = round((team_median_rate - my_rate) * my_bids)
    return _tip(
        "low_conversion", "bad",
        f"You get {my_rate * 100:.1f} interviews per 100 bids vs {team_median_rate * 100:.1f} for the team.",
        f"Over {my_bids} bids" + (f" that is about {missing} interview{'s' * (missing != 1)} fewer than the team median." if missing else
                                  " you are below the team median."),
        {"per100": my_rate * 100, "teamMedianPer100": team_median_rate * 100, "bids": my_bids}, missing)


def stalls_at_stage(mine: dict, team: dict) -> Optional[dict]:
    """`mine` / `team`: group → (passed, rejected). The worst group where both sides have ≥ MIN_DECIDED decided rounds."""
    best = None
    for group, (p, r) in mine.items():
        tp, tr = team.get(group, (0, 0))
        if p + r < MIN_DECIDED or tp + tr < MIN_DECIDED:
            continue
        me, them = _rate(p, r), _rate(tp, tr)
        if me < STALL_RATIO * them and (best is None or them - me > best[0]):
            best = (them - me, group, me, them, p + r)
    if not best:
        return None
    gap, group, me, them, decided = best
    label = _GROUP_LABEL.get(group, group)
    return _tip(
        "stalls_at_stage", "watch", f"{label}: you pass {me:.0%} vs team {them:.0%}.",
        f"{decided} decided {label.lower()}; this is where your interviews stall.",
        {"group": group, "myPassRate": me, "teamPassRate": them, "decided": decided}, gap * decided)


def caller_vs_self(caller: tuple, self_: tuple) -> Optional[dict]:
    if sum(caller) < MIN_DECIDED or sum(self_) < MIN_DECIDED:
        return None
    c, s = _rate(*caller), _rate(*self_)
    if c - s < CALLER_GAP:
        return None
    return _tip(
        "caller_vs_self", "watch",
        f"Caller-taken rounds pass {c:.0%} vs {s:.0%} when you take them — consider a caller for more of your rounds.",
        f"{sum(caller)} caller and {sum(self_)} self rounds with a result.",
        {"callerPassRate": c, "selfPassRate": s, "callerDecided": sum(caller), "selfDecided": sum(self_)}, (c - s) * sum(self_))


def up_vs_last_period(now: int, before: int, window: str = "the previous period") -> Optional[dict]:
    return frame.up_vs_last_period("interviews", "interviews", now, before, window)


async def interviews_view(subject, period: str, day: date, region: str, until: Optional[date], today: date) -> dict:
    from app.dashboard_stats import app_tz, local_day_start_utc
    from app.interview_rounds import held_round_results, load_interviews_for_window
    from app.routers.metrics import _display_name
    from app.stats.bids import team_counts

    start, end = interview_rates.window(period, day, until)
    days: dict = {}
    users, user_ids, regions, now = await team_counts(subject.id, start, end, region, days)
    bids = {uid: now[uid][0] if uid in now else 0 for uid in user_ids}
    ivs = {uid: now[uid][1] if uid in now else 0 for uid in user_ids}
    callers = {uid: now[uid][3] if uid in now else 0 for uid in user_ids}

    # Rounds again, with their results (counts() keeps only totals). ponytail: a second window query, merge into counts() if it shows up in profiles.
    tz = app_tz()
    first, last = local_day_start_utc(start, tz), local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    stage = {uid: defaultdict(lambda: [0, 0]) for uid in user_ids}  # group → [passed, rejected]
    who = {uid: {True: [0, 0], False: [0, 0]} for uid in user_ids}  # is_caller → [passed, rejected]
    processes = dict.fromkeys(user_ids, 0)
    tech = dict.fromkeys(user_ids, 0)
    final = dict.fromkeys(user_ids, 0)
    for iv in await load_interviews_for_window(user_ids, first, last):
        if region != "all" and regions.get(iv.accountId) != region:
            continue
        rounds = held_round_results(iv, first, last, tz)
        if not rounds:
            continue
        uid = iv.createdBy
        processes[uid] += 1
        groups = {g for _, g, _, _ in rounds}
        tech[uid] += bool(groups & TECH_OR_LATER)
        final[uid] += "final" in groups
        for _, group, is_caller, result in rounds:
            if result in ("passed", "rejected"):
                i = result == "rejected"
                stage[uid][group][i] += 1
                who[uid][is_caller][i] += 1

    team_ids = [uid for uid in users if ivs[uid] > 0 or bids[uid] >= MIN_BIDS]
    in_team = subject.id in team_ids

    def per100(uid) -> Optional[float]:
        return round(ivs[uid] / bids[uid] * 100, 1) if bids[uid] else None

    rated = [uid for uid in team_ids if bids[uid] >= MIN_BIDS]
    rates = [per100(u) for u in rated]
    counts_ = [ivs[u] for u in team_ids]
    reach = lambda uid: round(tech[uid] / processes[uid], 3)  # noqa: E731
    reach_ids = [u for u in team_ids if processes[u]]
    rates_d, counts_d = frame.distribution(rates), frame.distribution(counts_)
    reach_d = frame.distribution([reach(u) for u in reach_ids])

    me, sid = per100(subject.id), subject.id
    eligible = bids[sid] >= MIN_BIDS
    rank, of = frame.rank_of(me, rates, True, in_team) if eligible else (None, len(rates))
    i_rank, i_of = frame.rank_of(ivs[sid], counts_, True, in_team)
    my_reach = reach(sid) if processes[sid] else None
    r_rank, r_of = frame.rank_of(my_reach, [reach(u) for u in reach_ids], True, in_team) if my_reach is not None else (None, len(reach_ids))
    headline = [
        {"key": "per100", "label": "Interviews per 100 bids", "value": me, "format": "per100", "team": rates_d, "rank": rank,
         "of": of, "vsMedian": frame.vs_median(me, rates_d["median"]) if me is not None and eligible else None, "higherIsBetter": True,
         "note": (f"{ivs[sid]} interview{'s' * (ivs[sid] != 1)} from {bids[sid]} bids" if bids[sid]
                  else f"{ivs[sid]} interview{'s' * (ivs[sid] != 1)}, no bids yet")},
        {"key": "rank", "label": "Your rank", "value": rank, "format": "rank", "team": None, "rank": rank, "of": of,
         "vsMedian": None, "higherIsBetter": False, "note": f"of {of}" if eligible else f"needs {MIN_BIDS} bids"},
        {"key": "interviews", "label": "Interviews held", "value": ivs[sid], "format": "count", "team": counts_d,
         "rank": i_rank, "of": i_of, "vsMedian": frame.vs_median(ivs[sid], counts_d["median"]), "higherIsBetter": True,
         "note": f"Self {ivs[sid] - callers[sid]} · caller {callers[sid]}"},
        {"key": "reach", "label": "Reached tech or later", "value": my_reach, "format": "pct", "team": reach_d, "rank": r_rank,
         "of": r_of, "vsMedian": frame.vs_median(my_reach, reach_d["median"]) if my_reach is not None else None,
         "higherIsBetter": True, "note": f"final {round(100 * final[sid] / processes[sid])}%" if processes[sid] else ""},
    ]

    rows = []
    for uid in team_ids + ([] if in_team else [sid]):
        ok = bids[uid] >= MIN_BIDS
        r = frame.rank_of(per100(uid), rates, True, uid in team_ids)[0] if ok else None
        rows.append({"userId": str(uid), "name": _display_name(users.get(uid, subject)), "value": per100(uid),
                     "vsMedian": frame.vs_median(per100(uid), rates_d["median"]) if per100(uid) is not None and ok else None,
                     "rank": r, "parts": {"self": ivs[uid] - callers[uid], "caller": callers[uid]}, "isSubject": uid == sid})
    rows.sort(key=lambda r: (r["rank"] is None, -(r["value"] or 0) if r["rank"] else -(r["parts"]["self"] + r["parts"]["caller"]),
                             r["name"].lower()))

    trend = frame.trend(start, end, today, days, (2, 3), sid, team_ids)
    not_enough = frame.not_enough_data(len(team_ids), len(frame.working_days(start, end, today)))
    tips: list[dict] = []
    if not not_enough:
        team_stage = {g: tuple(sum(stage[u][g][i] for u in team_ids if u != sid) for i in (0, 1)) for g in _GROUP_LABEL}
        ps, pe = frame.previous_window(period, start, end, today)
        today_end = min(end, today)  # rounds scheduled later this period are not "so far"
        so_far = sum(v[2] + v[3] for (d, u), v in days.items() if u == sid and d <= today_end)
        before = (await interview_rates.counts([sid], ps, pe, region, regions, logged_only=False)).get(sid, (0, 0))[1]
        candidates = [
            low_conversion(ivs[sid] / bids[sid] if bids[sid] else None, rates_d["median"] / 100 if rates_d["median"] else None, bids[sid]),
            stalls_at_stage({g: tuple(v) for g, v in stage[sid].items()}, team_stage),
            caller_vs_self(tuple(who[sid][True]), tuple(who[sid][False])),
            up_vs_last_period(so_far, before, frame.span_label(ps, pe)),
        ]
        if not ivs[sid]:
            candidates.append(_tip("no_interviews", "bad", "No interviews yet this period.",
                                   f"The team median is {round(counts_d['median'] or 0)} interviews so far.",
                                   {"teamMedian": counts_d["median"]}, counts_d["median"] or 0))
        tips = frame.order_tips([t for t in candidates if t])

    return {
        "tab": "interviews", "label": interview_rates.label(period, start, end),
        "subject": {"userId": str(sid), "name": _display_name(subject)},
        "headline": headline, "tips": tips, "everyone": {"rowKind": "person", "metric": "per100", "rows": rows},
        "trend": trend, "notEnoughData": not_enough, "definitions": DEFINITIONS,
    }
