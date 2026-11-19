"""Per-profile application summaries, team baseline and grades for the Statistics "Your profiles" card (pure, no DB)."""
from datetime import datetime, timedelta
from statistics import median, median_low
from typing import Optional

MIN_MATURED = 30
DEAD_MATURED = 150
SILENT_DAYS = 21
STILL_BIDDING_DAYS = 7
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
        s["bidsSelf" if r.get("by") == "self" else "bidsBidder"] += 1
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
    rates = [s["per100"] for s in summaries.values() if s["matured"] >= MIN_MATURED and s["per100"] is not None]
    d = [r["daysToInterview"] for r in rows
         if r.get("bidStatus") != "rejected" and r.get("interviewId") and r.get("daysToInterview") is not None]
    return {"per100": round(median(rates), 1) if rates else None, "medianDays": median_low(d) if d else None,
            "profiles": len(rates)}


def grade(summary: dict, team: dict, activity: Optional[dict], matured_days: int, now: datetime) -> tuple[str, list[str]]:
    """dead -> early -> strong -> watch. `activity` = {firstAt, lastAt, lastRoundAt} over all the profile's rows."""
    if summary["matured"] >= DEAD_MATURED and summary["maturedInterviews"] == 0:
        return "dead", [f"{DEAD_MATURED}+ matured bids, no interview"]
    if activity and activity.get("firstAt") and activity.get("lastAt"):
        bidding = activity["lastAt"] >= now - timedelta(days=STILL_BIDDING_DAYS)
        silent = not activity.get("lastRoundAt") or activity["lastRoundAt"] < now - timedelta(days=SILENT_DAYS)
        old = activity["firstAt"] < now - timedelta(days=matured_days + SILENT_DAYS)
        if bidding and silent and old:
            return "dead", [f"no interview for {SILENT_DAYS}+ days"]
    if summary["matured"] < MIN_MATURED or team["per100"] is None:
        return "early", ["too early to judge"]
    per100 = summary["per100"] or 0.0
    reach = summary["reach"]
    if per100 >= team["per100"] and reach["tech"] > 0:
        return "strong", []
    why = []
    if per100 < team["per100"]:
        why.append(f"below team ({per100} vs {team['per100']} per 100)")
    med, tmed = (summary["days"] or {}).get("median"), team.get("medianDays")
    if med and tmed and med >= SLOW_RATIO * tmed:
        why.append(f"slow (median {med} days vs team {tmed})")
    if summary["interviews"] >= STUCK_INTERVIEWS and reach["tech"] == 0:
        why.append("intros don't reach tech")
    return "watch", why
