""""What to change": one suggestion per pattern a profile shows (pure, no DB). Copy per the slices 3-5 plan."""
from datetime import datetime, timedelta
from typing import Optional

from app.stats.job_fit import BOARD_LABELS, breakdown
from app.stats.profile_report import EXPECTED_MIN, MIN_MATURED, SLOW_RATIO, _per100

MAX_SUGGESTIONS = 3
QUIET_DAYS = 21
RECENT_DAYS = 7
LOW_SCORE_GAP = 5
MIN_SCORED = 10
VOLUME_STEP = 0.25
MIN_SLOW = 3  # interviews: no speed advice from one or two data points


def _avg(xs: list) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def _best(groups: list[dict], skip: set) -> list[dict]:
    """Groups by team rate, best first, with their evidence; only those with 30+ matured bids and 3+ team interviews
    (at ~0.5 per 100 a group with 1-2 interviews tops the list by chance)."""
    ok = [g for g in groups if g["key"] not in skip and not g["team"]["tooFew"]
          and g["team"]["interviews"] >= EXPECTED_MIN]
    return [{"label": g["label"], "interviews": g["team"]["interviews"], "matured": g["team"]["matured"]}
            for g in sorted(ok, key=lambda g: -g["team"]["interviews"] / g["team"]["matured"])]


def team_context(rows: list[dict], matured_before: datetime, team: dict) -> dict:
    """Team numbers every profile's suggestions compare against: avg score, best 2 sites, best score band."""
    b = breakdown([], rows, matured_before)
    scores = [r["score"] for r in rows if r.get("bidStatus") != "rejected" and r.get("score") is not None]
    bands = _best(b["byScore"], {None})
    return {"team": {**team, "avgScore": _avg(scores)}, "bestSites": _best(b["bySite"], {"other", "none"})[:2],
            "bestBand": bands[0] if bands else None}


def profile_context(rows: list[dict], matured_before: datetime) -> dict:
    """One profile's rows -> scores, self / bidder split, the site to drop and its busiest bidder."""
    rows = [r for r in rows if r.get("bidStatus") != "rejected"]
    matured = [r for r in rows if r["appliedAt"] <= matured_before]
    split, sites, bidders = {}, {}, {}
    for by in ("self", "bidder"):
        m = [r for r in matured if (r.get("by") == "self") == (by == "self")]
        iv = sum(1 for r in m if r.get("interviewId"))
        split[by] = {"matured": len(m), "interviews": iv, "per100": _per100(iv, len(m))}
    for r in matured:
        if r.get("board"):
            s = sites.setdefault(r["board"], [0, 0])
            s[0] += 1
            s[1] += bool(r.get("interviewId"))
        if r.get("by") == "bidder" and r.get("bidderId"):
            bidders[r["bidderId"]] = bidders.get(r["bidderId"], 0) + 1
    dead_sites = sorted((b for b, (n, iv) in sites.items() if iv == 0), key=lambda b: (-sites[b][0], b))
    scores = [r["score"] for r in rows if r.get("score") is not None]
    return {"scores": {"avg": _avg(scores), "n": len(scores)}, "split": split,
            "deadSites": [{"label": BOARD_LABELS.get(b, b), "matured": sites[b][0]} for b in dead_sites],
            "topBidderId": max(bidders, key=lambda k: (bidders[k], str(k)), default=None)}


def quiet_candidate(rows: list[dict], now: datetime, matured_days: int) -> bool:
    """Still bidding (a non-rejected row in the last 7 days) and the profile older than matured_days + 21 (its
    createdAt from appliedAt - profileAgeDays; the first row in the period when no row knows the age)."""
    born = [r["appliedAt"] - timedelta(days=r["profileAgeDays"]) for r in rows if r.get("profileAgeDays") is not None]
    return (bool(rows) and min(born or [r["appliedAt"] for r in rows]) <= now - timedelta(days=matured_days + QUIET_DAYS)
            and any(r["appliedAt"] >= now - timedelta(days=RECENT_DAYS) for r in rows
                    if r.get("bidStatus") != "rejected"))


def _expected(matured: int, team_per100: Optional[float]) -> float:
    return matured * (team_per100 or 0) / 100


def _below(s: dict, ctx: dict) -> bool:
    t = ctx["team"].get("rate", ctx["team"]["per100"])
    return t is not None and _expected(s["matured"], t) >= EXPECTED_MIN and (s["per100"] or 0) < t


def _out(pattern: str, text: str, **evidence) -> dict:
    return {"pattern": pattern, "text": text, "evidence": evidence}


def dead(s, g, ctx):
    if g == "dead":
        return _out("dead", f"Archive it, or rebuild it with a new base resume / prompt and judge it fresh after "
                            f"{ctx['matured_days']} days.", matured=s["matured"], maturedDays=ctx["matured_days"])


def _low_scores(ctx) -> bool:
    sc, team_avg = ctx["scores"], ctx["team"].get("avgScore")
    return sc["n"] >= MIN_SCORED and team_avg is not None and sc["avg"] < team_avg - LOW_SCORE_GAP


def low_scores(s, g, ctx):
    if _below(s, ctx) and _low_scores(ctx):
        sc, team_avg = ctx["scores"], ctx["team"]["avgScore"]
        return _out("low_scores", f"Low interview rate and low resume scores (avg {round(sc['avg'])} vs team "
                                  f"{round(team_avg)}) — improve the base resume or the role's prompt; check it on "
                                  f"Prompt tests.", avgScore=sc["avg"], scored=sc["n"], teamAvgScore=team_avg)


def wrong_sites(s, g, ctx):
    sites = ctx["bestSites"]
    if not sites or not _below(s, ctx) or _low_scores(ctx):
        return None
    # drop a site only when the team rate would have given it 3+ interviews and it got none
    worst = next((d for d in ctx["deadSites"] if d["label"] not in {s["label"] for s in sites}
                  and _expected(d["matured"], ctx["team"]["per100"]) >= EXPECTED_MIN), None)
    drop = f"; drop {worst['label']}" if worst else ""
    sc, team_avg = ctx["scores"], ctx["team"].get("avgScore")
    good = sc["n"] >= MIN_SCORED and team_avg is not None and sc["avg"] >= team_avg
    lead = "Good resumes, wrong places" if good else "Wrong places"
    where = [f"{s['label']} ({s['interviews']:,} interviews from {s['matured']:,} matured bids)" if i == 0
             else f"{s['label']} ({s['interviews']:,} from {s['matured']:,})" for i, s in enumerate(sites)]
    return _out("wrong_sites", f"{lead} — move bids to {' and '.join(where)}, where the team "
                               f"converts best{drop}.", bestSites=sites, worstSite=worst)


def stuck_intro(s, g, ctx):
    if "intros don't reach tech" in ctx["reasons"]:
        return _out("stuck_intro", "Intros don't turn into tech rounds — tone the prompt down to real skills and add "
                                   "interview prep.", interviews=s["interviews"], tech=s["reach"]["tech"])


def slow(s, g, ctx):
    med, tmed = (s["days"] or {}).get("median"), ctx["team"]["medianDays"]
    n = s["interviews"]
    if n >= MIN_SLOW and tmed is not None and tmed >= 1 and med is not None and med >= SLOW_RATIO * tmed:
        return _out("slow", f"Interviews come slowly ({med} days vs team {tmed}, from {n} interviews) — sharpen the "
                            f"summary; try a winning profile's template.", medianDays=med, teamMedianDays=tmed, interviews=n)


def bidder_gap(s, g, ctx):
    me, bd = ctx["split"]["self"], ctx["split"]["bidder"]
    if me["matured"] < MIN_MATURED or bd["matured"] < MIN_MATURED or me["interviews"] < EXPECTED_MIN:
        return None
    if me["per100"] < 2 * bd["per100"] or _expected(bd["matured"], me["per100"]) < EXPECTED_MIN:
        return None
    return _out("bidder_gap", f"Your own applies convert better than your bidders' ({me['interviews']} of "
                              f"{me['matured']} vs {bd['interviews']} of {bd['matured']} matured bids) — review "
                              f"{ctx['bidder'] or 'your top bidder'}'s jobs and rejected share.", self=me, bidder=bd)


def volume_up_rate_down(s, g, ctx):
    prev, t = ctx["prev"], ctx["team"]["per100"]
    if not prev or not prev["per100"] or s["per100"] is None:
        return None
    if min(_expected(s["matured"], t), _expected(prev["matured"], t)) < EXPECTED_MIN:
        return None
    if s["matured"] < (1 + VOLUME_STEP) * prev["matured"] or s["per100"] > (1 - VOLUME_STEP) * prev["per100"]:
        return None
    bb = ctx["bestBand"]
    band = (f" (score {bb['label']} converts best for the team: {bb['interviews']:,} interviews from "
            f"{bb['matured']:,})") if bb else ""
    return _out("volume_up_rate_down", f"More bids, fewer interviews per bid — fewer, better-matched applies{band}.",
                matured=s["matured"], per100=s["per100"], prevMatured=prev["matured"], prevPer100=prev["per100"])


def quiet_now(s, g, ctx):
    if ctx["quietNow"]:
        return _out("quiet_now", "No interview on this profile for 21+ days while it's still bidding — check the base "
                                 "resume and the jobs it's applied to.", days=QUIET_DAYS)


PATTERNS = (dead, low_scores, wrong_sites, stuck_intro, slow, bidder_gap, volume_up_rate_down, quiet_now)
NEGATIVE = {low_scores, wrong_sites, slow, bidder_gap, volume_up_rate_down}


def suggest(profile: dict, summary: dict, grade: str, ctx: dict) -> list[dict]:
    if grade == "dead":  # archive or rebuild: any other advice would contradict it
        return [dead(summary, grade, ctx)]
    out = []
    for p in PATTERNS:
        if grade == "early" and p in NEGATIVE:
            continue
        hit = p(summary, grade, ctx)
        if hit:
            out.append(hit)
    return out[:MAX_SUGGESTIONS]
