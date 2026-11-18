"""Statistics → AI usage: the subject's LLM spend (per resume, per interview) against the team."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta, timezone
from statistics import median
from typing import Optional

from app import interview_rates
from app.stats import frame

EXPENSIVE_RATIO = 2
DEFINITIONS = [
    "AI usage counts every billed LLM call made for you in the period; calls without a catalog price count as $0.",
    "Cost per resume is the spend on tailored-resume calls (retries included) over the resumes that succeeded.",
    "AI cost per interview is your total AI spend over the interviews you held in the period.",
]


def _usd(v: float) -> str:
    return f"${abs(v):,.0f}" if abs(v) >= 10 else f"${abs(v):.2f}"


def expensive_model(my_cpr: Optional[float], team_median_cpr: Optional[float], my_top_model: Optional[str],
                    team_top: Optional[tuple[str, float]]) -> Optional[dict]:
    """`team_top` = (the team's most-used resume model, that model's team cost per resume)."""
    if not my_cpr or not team_median_cpr or not team_top or my_cpr < EXPENSIVE_RATIO * team_median_cpr:
        return None
    model, model_cpr = team_top
    if model == my_top_model or model_cpr >= my_cpr:
        return None
    return frame.tip("ai_usage.expensive_model", "watch",
                     f"Your resumes cost {_usd(my_cpr)} each vs {_usd(team_median_cpr)} for the team — most of the team uses {model}.",
                     f"{_usd(my_cpr)} per resume with {my_top_model or 'your models'}; {model} costs the team {_usd(model_cpr)} per resume.",
                     {"costPerResume": my_cpr, "teamMedian": team_median_cpr, "model": model}, my_cpr - model_cpr)


def _ratio(a: float, b: int) -> Optional[float]:
    return a / b if b else None


async def ai_usage_view(subject, period: str, day: date, region: str, until: Optional[date], today: date) -> dict:
    from app.dashboard_stats import app_tz, local_day_start_utc
    from app.models import AiUsageEvent
    from app.routers.dashboard import _active_members
    from app.routers.metrics import _display_name

    start, end = interview_rates.window(period, day, until)
    tz = app_tz()
    members = {u.id: u for u in await _active_members()}
    ids = list(members) + ([] if subject.id in members else [subject.id])
    events = await AiUsageEvent.find({
        "userId": {"$in": ids},
        "createdAt": {"$gte": local_day_start_utc(start, tz), "$lt": local_day_start_utc(end + timedelta(days=1), tz)}}).to_list()

    cost: dict = defaultdict(float)
    calls: dict = defaultdict(int)
    unpriced: dict = defaultdict(int)
    resume_cost: dict = defaultdict(float)
    refs: dict = defaultdict(set)
    models: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))  # uid → model → [resume cost, resume events]
    days: dict = defaultdict(lambda: (0.0,))
    for e in events:
        c = e.costUsd or 0.0
        cost[e.userId] += c
        calls[e.userId] += e.calls
        unpriced[e.userId] += e.calls if e.costUsd is None else 0
        d = e.createdAt.replace(tzinfo=timezone.utc).astimezone(tz).date() if e.createdAt.tzinfo is None else e.createdAt.astimezone(tz).date()
        days[(d, e.userId)] = (days[(d, e.userId)][0] + c,)
        if e.feature == "resume":
            resume_cost[e.userId] += c
            m = models[e.userId][e.model]
            m[0] += c
            m[1] += 1
            if e.ok and e.refId:
                refs[e.userId].add(e.refId)

    team_ids = [u for u in members if cost[u] > 0]
    in_team = subject.id in team_ids
    regions, _ = await interview_rates._profile_regions()
    held = await interview_rates.counts(ids, start, end, "all", regions, logged_only=False)
    interviews = {u: held[u][1] if u in held else 0 for u in ids}
    cpr = {u: _ratio(resume_cost[u], len(refs[u])) for u in ids}
    cpi = {u: _ratio(cost[u], interviews[u]) for u in ids}

    def block(key, label, values, note, subj_val):
        team_vals = [round(values[u], 4) for u in team_ids if values[u] is not None]
        dist = frame.distribution(team_vals)
        rank, of = (frame.rank_of(round(subj_val, 4), team_vals, False, in_team)
                    if subj_val is not None else (None, len(team_vals)))
        return {"key": key, "label": label, "value": None if subj_val is None else round(subj_val, 4), "format": "usd",
                "team": dist, "rank": rank, "of": of, "vsMedian": None if subj_val is None else frame.vs_median(subj_val, dist["median"]),
                "higherIsBetter": False, "note": note}

    sid = subject.id
    costs = [round(cost[u], 4) for u in team_ids]
    note = f"{calls[sid]} calls" + (f" · {unpriced[sid]} unpriced" if unpriced[sid] else "")
    cost_h = block("cost", "Your AI cost", cost, note, cost[sid])
    headline = [
        cost_h,
        {"key": "rank", "label": "Your rank", "value": cost_h["rank"], "format": "rank", "team": None, "rank": cost_h["rank"],
         "of": cost_h["of"], "vsMedian": None, "higherIsBetter": False, "note": f"of {cost_h['of']}"},
        block("perResume", "Cost per resume", cpr, f"{len(refs[sid])} resumes", cpr[sid]),
        block("perInterview", "AI cost per interview", cpi, f"{interviews[sid]} interviews", cpi[sid]),
    ]

    dist = cost_h["team"]
    rows = []
    for uid in team_ids + ([] if in_team else [sid]):
        r, _ = frame.rank_of(round(cost[uid], 4), costs, False, uid in team_ids)
        rows.append({"userId": str(uid), "name": _display_name(members.get(uid, subject)), "value": round(cost[uid], 4),
                     "vsMedian": frame.vs_median(cost[uid], dist["median"]), "rank": r,
                     "parts": {"resume": round(resume_cost[uid], 4), "other": round(cost[uid] - resume_cost[uid], 4)},
                     "isSubject": uid == sid})
    rows.sort(key=lambda r: (r["value"], r["name"].lower()))

    trend = frame.trend(start, end, today, days, (0,), sid, team_ids)
    for b in trend["buckets"]:
        b["you"], b["median"] = round(b["you"], 4), round(b["median"], 4)

    n_days = len(frame.working_days(start, end, today))
    not_enough = frame.not_enough_data(len(team_ids), n_days)
    tips: list[dict] = []
    if not not_enough:
        by_model: dict = defaultdict(lambda: [0.0, 0])
        for u in team_ids:
            for name, (c, n) in models[u].items():
                by_model[name][0] += c
                by_model[name][1] += n
        top = max(by_model, key=lambda k: by_model[k][1]) if by_model else None
        mine = models[sid]
        my_top = max(mine, key=lambda k: mine[k][1]) if mine else None
        team_cprs = [v for u in team_ids if (v := cpr[u]) is not None]
        tips = frame.order_tips([t for t in (expensive_model(
            cpr[sid], median(team_cprs) if team_cprs else None, my_top,
            (top, by_model[top][0] / by_model[top][1]) if top else None),) if t])

    return {
        "tab": "ai-usage", "label": interview_rates.label(period, start, end),
        "subject": {"userId": str(sid), "name": _display_name(subject)},
        "headline": headline, "tips": tips, "everyone": {"rowKind": "person", "metric": "cost", "rows": rows},
        "trend": trend, "notEnoughData": not_enough, "definitions": DEFINITIONS,
    }
