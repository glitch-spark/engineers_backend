"""Profiles list banners (spec 2026-11-14-profiles-flags-and-top-design.md): the owner's profiles that got no
interview in the last 2 weeks (red, with their 2-week numbers for the suggested action) and those with no applies,
self or bidder, in the last 3 weeks (grey, archive suggestion). Both windows end today and ignore the page's date
filter; a profile too new for a window is never flagged by it, and a grey one is never also red. Each flagged profile
also carries how long it has gone without (idleDays: since its last interview / apply in the lookback, else since it
was created; capped when the whole lookback is empty)."""
from __future__ import annotations

from datetime import date, timedelta

from app import profile_compare as pc
from app import profile_metrics as pm

# ponytail: one load over this lookback for the idle counts; longer gaps say "over 26 weeks". Raise it if that matters.
LOOKBACK_DAYS = 182


def _idle(created: date, today: date, look_from: date, daily: dict, keys: tuple[str, ...]) -> dict:
    """{idleDays, capped}: days since the last day any of `keys` counted, else since creation (or the lookback)."""
    last = max((d for d, c in daily.items() if any(c.get(k, 0) for k in keys)), default=None)
    since = last or max(created, look_from)
    return {"idleDays": (today - since).days, "capped": last is None and created < look_from}


async def profile_alerts(accounts: list[pm.ProfileSpan], today: date) -> dict:
    """{asOf, noInterviews: [{accountId, name, bids, tailored, tailorScore}] most bids first, allTailorScore: all
    users' tailor average over the same 14 days when the benchmark cache has it (never waited for), noApplies:
    [{accountId, name}] by name}."""
    two = (today - timedelta(days=13), today)
    three = (today - timedelta(days=20), today)
    bench = pc.benchmark_cache.peek(two)
    out: dict = {"asOf": today.isoformat(), "noInterviews": [], "noApplies": [],
                 "allTailorScore": ((bench or {}).get("tailorScore") or {}).get("avg")}
    if not accounts:
        return out
    look_from = today - timedelta(days=LOOKBACK_DAYS - 1)
    sources = await pm.load_sources(accounts, look_from, today)
    for a in accounts:
        key, created = str(a.id), pc.created_day(a)
        if created > two[0]:
            continue
        history, _ = pm.days_from(key, look_from, today, sources)
        if created <= three[0]:
            daily, scores = pm.days_from(key, *three, sources)
            if pc.window_stats(created, three, daily, scores, [])["bids"] == 0:
                out["noApplies"].append({"accountId": key, "name": a.name,
                                         **_idle(created, today, look_from, history, ("selfBids", "bidderBids"))})
                continue
        daily, scores = pm.days_from(key, *two, sources)
        s = pc.window_stats(created, two, daily, scores, [])
        if s["interviews"] == 0:
            out["noInterviews"].append({
                "accountId": key, "name": a.name, "bids": s["bids"], "tailored": s["tailored"],
                "tailorScore": pc._score(s["tailorScores"]),
                **_idle(created, today, look_from, history, ("selfInterviews", "callerInterviews")),
            })
    out["noInterviews"].sort(key=lambda x: (-x["bids"], x["name"].lower()))
    out["noApplies"].sort(key=lambda x: x["name"].lower())
    return out
