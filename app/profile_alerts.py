"""Profiles list banners (spec 2026-11-14-profiles-flags-and-top-design.md): the owner's profiles that got no
interview in the last 2 weeks (red, with their 2-week numbers for the suggested action) and those with no applies,
self or bidder, in the last 3 weeks (grey, archive suggestion). Both windows end today and ignore the page's date
filter; a profile too new for a window is never flagged by it, and a grey one is never also red."""
from __future__ import annotations

from datetime import date, timedelta

from app import profile_compare as pc
from app import profile_metrics as pm


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
    sources = await pm.load_sources(accounts, *three)
    for a in accounts:
        key, created = str(a.id), pc.created_day(a)
        if created <= three[0]:
            daily, scores = pm.days_from(key, *three, sources)
            if pc.window_stats(created, three, daily, scores, [])["bids"] == 0:
                out["noApplies"].append({"accountId": key, "name": a.name})
                continue
        if created <= two[0]:
            daily, scores = pm.days_from(key, *two, sources)
            s = pc.window_stats(created, two, daily, scores, [])
            if s["interviews"] == 0:
                out["noInterviews"].append({"accountId": key, "name": a.name, "bids": s["bids"],
                                            "tailored": s["tailored"], "tailorScore": pc._score(s["tailorScores"])})
    out["noInterviews"].sort(key=lambda x: (-x["bids"], x["name"].lower()))
    out["noApplies"].sort(key=lambda x: x["name"].lower())
    return out
