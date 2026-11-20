"""Per-profile application summaries, team baseline and grades for the Statistics "Your profiles" card (pure, no DB)."""
from datetime import datetime
from statistics import median_low
from typing import Optional

MIN_MATURED = 30
EXPECTED_MIN = 3  # judge a shortfall only when the team rate would give 3+ interviews (P(0 by chance) < 5%)
SLOW_RATIO = 2
STUCK_INTERVIEWS = 3
_TECH = {"tech", "hiring", "panel", "final", "offer"}
_FINAL = {"final", "offer"}


def _per100(interviews: int, matured: int) -> Optional[float]:
    return round(interviews / matured * 100, 1) if matured else None


def summarize(rows: list[dict], matured_before: datetime) -> dict[str, dict]:
    out: dict[str, dict] = {}
    days: dict[str, list[int]] = {}
    for r in rows:
        if r.get("bidStatus") == "rejected" or not r.get("accountId"):
            continue
        key = str(r["accountId"])
        s = out.setdefault(key, {"bidsSelf": 0, "bidsBidder": 0, "matured": 0, "maturedInterviews": 0, "interviews": 0,
                                 "reach": {"intro": 0, "tech": 0, "final": 0}, "stillInPlay": 0})
        if r.get("by") in ("self", "bidder"):  # "unknown" (interview-only rows) is neither
            s["bidsSelf" if r["by"] == "self" else "bidsBidder"] += 1
        matured = r["appliedAt"] <= matured_before
        has_iv = bool(r.get("interviewId"))
        s["matured"] += matured
        if has_iv:
            s["interviews"] += 1
            s["maturedInterviews"] += matured
            s["reach"]["intro"] += 1
            s["reach"]["tech"] += r.get("furthest") in _TECH
            s["reach"]["final"] += r.get("furthest") in _FINAL
            if r.get("daysToInterview") is not None:
                days.setdefault(key, []).append(r["daysToInterview"])
        elif not matured:
            s["stillInPlay"] += 1
    for key, s in out.items():
        d = days.get(key)
        s["per100"] = _per100(s["maturedInterviews"], s["matured"])
        s["days"] = {"first": min(d), "median": median_low(d), "slowest": max(d)} if d else None
    return out


def team_baseline(summaries: dict[str, dict], rows: list[dict]) -> dict:
    # pooled team rate (changed 2026-11-18 after real data: the median of profiles collapsed to 0);
    # "rate" stays unrounded for expected-interview maths, "per100" is the rounded display value
    total = sum(s["matured"] for s in summaries.values())
    rate = sum(s["maturedInterviews"] for s in summaries.values()) / total * 100 if total else None
    d = [r["daysToInterview"] for r in rows
         if r.get("bidStatus") != "rejected" and r.get("interviewId") and r.get("daysToInterview") is not None]
    return {"per100": round(rate, 1) if rate is not None else None, "rate": rate,
            "medianDays": median_low(d) if d else None,
            "profiles": sum(1 for s in summaries.values() if s["matured"] >= MIN_MATURED)}


def grade(summary: dict, team: dict) -> tuple[str, list[str]]:
    """early -> dead -> strong -> watch -> early (too few to tell). User decision 2026-11-18."""
    matured = summary["matured"]
    if matured < MIN_MATURED:
        return "early", [f"too early to judge ({matured} of {MIN_MATURED} matured bids)"]
    if team["per100"] is None:
        return "early", [f"no team baseline yet (fewer than {MIN_MATURED} matured bids on every profile)"]
    rate = team.get("rate", team["per100"])
    expected = matured * rate / 100
    if expected >= EXPECTED_MIN and summary["maturedInterviews"] == 0:
        return "dead", [f"no interview from {matured} matured bids (the team rate would give about {round(expected)})"]
    per100 = summary["per100"] or 0.0
    reach = summary["reach"]
    if per100 >= rate and reach["tech"] > 0:
        return "strong", []
    why = []
    if expected >= EXPECTED_MIN and per100 < rate:
        why.append(f"below team ({per100} vs {team['per100']} per 100)")
    med, tmed = (summary["days"] or {}).get("median"), team.get("medianDays")
    if tmed is not None and tmed >= 1 and med is not None and med >= SLOW_RATIO * tmed:
        why.append(f"slow (median {med} days vs team {tmed})")
    if summary["interviews"] >= STUCK_INTERVIEWS and reach["tech"] == 0:
        why.append("intros don't reach tech")
    elif per100 >= rate and summary["interviews"] and reach["tech"] == 0:
        why.append("no interview has reached tech yet")
    if why:
        return "watch", why
    return "early", [f"too few to tell ({matured} matured bids; about {expected:.1f} interviews expected at the team rate)"]
