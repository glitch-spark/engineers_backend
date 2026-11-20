"""Statistics → Conversion `summary`: top cards, the Everyone list and the coverage line
(docs/superpowers/specs/2026-11-19-conversion-redesign-design.md, Key definitions).

Bids are the Bids frame's (Plans + Bid Track); interviews are interviews STARTED in the period (first non-canceled
round, per interview_outcome) owned by their creator, on a profile of the region (current region, archived included).
Median / rank over team members with >= 1 bid; per 100 bids ranked only from MIN_BIDS bids."""
from __future__ import annotations

from collections import Counter
from typing import Optional

from app.interview_rates import MIN_BIDS
from app.stats import frame
from app.stats.bids import team_counts
from app.stats.profile_loader import interviews_started


def _per100(p: dict) -> Optional[float]:
    return round(p["interviews"] / p["bids"] * 100, 1) if p["bids"] else None


def build(people: dict, subject_id, team_view: bool, rows: int, matched: int) -> dict:
    """`people`: uid → {name, bids, bidder, interviews, rounds, member}; the subject is always in it.
    `rows` / `matched`: the conversion report's job-level bids and interviews (coverage)."""
    members = [u for u, p in people.items() if p["member"]]
    pop = [u for u in members if people[u]["bids"] > 0]
    rated = [u for u in pop if people[u]["bids"] >= MIN_BIDS]
    bids_v, ivs_v = [people[u]["bids"] for u in pop], [people[u]["interviews"] for u in pop]
    rates_v = [_per100(people[u]) for u in rated]
    bids_med, ivs_med = frame.distribution(bids_v)["median"], frame.distribution(ivs_v)["median"]
    total = {k: sum(people[u][k] for u in members) for k in ("bids", "bidder", "interviews", "rounds")}
    team_rate = _per100(total)

    def ranks(u) -> dict:
        p, in_pop = people[u], u in pop
        return {"bids": frame.rank_of(p["bids"], bids_v, True, in_pop),
                "interviews": frame.rank_of(p["interviews"], ivs_v, True, in_pop),
                "per100": frame.rank_of(_per100(p), rates_v, True, u in rated) if p["bids"] >= MIN_BIDS
                else (None, len(rates_v))}

    if team_view:
        cards = {
            "bids": {"value": total["bids"], "self": total["bids"] - total["bidder"], "bidder": total["bidder"],
                     "rank": None, "of": None, "median": bids_med, "vsMedian": None},
            "interviews": {"value": total["interviews"], "rounds": total["rounds"], "rank": None, "of": None,
                           "median": ivs_med, "vsMedian": None},
            "per100": {"value": team_rate, "rank": None, "of": None, "teamRate": team_rate},
        }
        me = total
    else:
        me, r = people[subject_id], ranks(subject_id)
        cards = {
            "bids": {"value": me["bids"], "self": me["bids"] - me["bidder"], "bidder": me["bidder"],
                     "rank": r["bids"][0], "of": r["bids"][1], "median": bids_med,
                     "vsMedian": frame.vs_median(me["bids"], bids_med)},
            "interviews": {"value": me["interviews"], "rounds": me["rounds"], "rank": r["interviews"][0],
                           "of": r["interviews"][1], "median": ivs_med,
                           "vsMedian": frame.vs_median(me["interviews"], ivs_med)},
            "per100": {"value": _per100(me), "rank": r["per100"][0], "of": r["per100"][1], "teamRate": team_rate},
        }

    everyone = []
    for u, p in people.items():
        if not (p["bids"] or p["interviews"]):
            continue
        r = ranks(u)
        everyone.append({"userId": str(u), "name": p["name"], "bids": p["bids"], "interviews": p["interviews"],
                         "per100": _per100(p) if p["bids"] >= MIN_BIDS else None, "isSubject": u == subject_id,
                         "ranks": {k: v[0] for k, v in r.items()}})
    everyone.sort(key=lambda e: (-e["interviews"], -e["bids"], e["name"].lower()))
    return {"cards": cards, "everyone": everyone,
            "coverage": {"rows": rows, "bids": me["bids"], "matched": matched, "interviews": me["interviews"]}}


async def summary(sc: dict, region: str, totals: dict) -> dict:
    """`sc`: conversion_scope's result; `totals`: the conversion report's totals. scope=profile reads as the subject."""
    from app.routers.metrics import _display_name

    sid = sc["sid"]
    users, user_ids, regions, now = await team_counts(sid, sc["start_d"], sc["end_d"], region)
    started = Counter(
        d["createdBy"] for d in await interviews_started({"createdBy": {"$in": user_ids}}, sc["start"], sc["end"])
        if region == "all" or regions.get(d["accountId"]) == region)
    people = {}
    for u in user_ids:
        bids, rounds, bidder, _ = now.get(u, (0, 0, 0, 0))
        people[u] = {"name": _display_name(users[u]) if u in users else sc["subject"]["name"], "bids": bids,
                     "bidder": bidder, "interviews": started[u], "rounds": rounds, "member": u in users}
    return build(people, sid, sc["scope"] == "team", totals["bids"], totals["interviewsAll"])
