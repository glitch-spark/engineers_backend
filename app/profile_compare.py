"""Profile → Metrics comparisons: this profile, the owner's other profiles and every owner's profiles, measured the
same fair way.

- The compared window starts no earlier than the launch day (`profile_metrics.since_date`): before it self bids
  weren't recorded per profile, and tailor scores were measured differently.
- A profile counts from its own creation day inside the window, so a young profile isn't diluted by weeks it didn't
  exist; one created after the window isn't there at all.
- Averages keep only profiles with at least one bid in the window (idle profiles would pull them toward zero).
- Volumes (per week) are means over those profiles; rates and scores are pooled, so a profile with 3 bids doesn't
  weigh like one with 300.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from app import profile_metrics as pm
from app.interview_rates import rate

COMPARE_KEYS = (
    "profiles", "bids", "decided", "bidsPerWeek", "interviewsPerWeek", "tailoredResumesPerWeek", "interviewsPerBid",
    "passRate", "tailorScore", "matchScore",
)


async def compared_window(start: date, end: date) -> Optional[tuple[date, date]]:
    """[max(start, launch day), end]; None when the launch day comes after `end` (nothing to compare). No launch day
    recorded: the range as is."""
    since = await pm.since_date()
    first = max(start, since) if since else start
    return (first, end) if first <= end else None


def window_stats(
    created: date, window: tuple[date, date], daily: dict[date, dict], score_lists: dict, rounds: list[dict],
) -> Optional[dict]:
    """One profile's raw numbers over its days in the window (from its creation day on); None when it was created
    after the window. `daily`: day -> counts (`profile_metrics._COUNTS` keys) covering at least those days;
    `score_lists`: already cut to the window; `rounds`: `profile_rounds` records (filtered here by day; home
    assessments never count toward passed / rejected)."""
    first, last = max(window[0], created), window[1]
    if first > last:
        return None
    days = [c for d, c in daily.items() if first <= d <= last]
    mine = [r for r in rounds if first <= r["day"] <= last and r["type"] != "home_assessment"]
    return {
        "weeks": -(-((last - first).days + 1) // 7),
        "bids": sum(c.get("selfBids", 0) + c.get("bidderBids", 0) for c in days),
        "interviews": sum(c.get("selfInterviews", 0) + c.get("callerInterviews", 0) for c in days),
        "tailored": score_lists.get("tailoredResumes", 0),
        "tailorScores": list(score_lists.get("tailored", [])),
        "matchScores": list(score_lists.get("match", [])),
        "passed": sum(1 for r in mine if r["result"] == "passed"),
        "rejected": sum(1 for r in mine if r["result"] == "rejected"),
    }


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def _score(values: list[int]) -> dict:
    return {"avg": _mean(values), "n": len(values)}


def pool(stats: list[Optional[dict]], *, drop_idle: bool = True) -> Optional[dict]:
    """CompareStats (`COMPARE_KEYS`) of these `window_stats`: per-week values are means over the profiles, rates and
    scores pooled. Absent profiles (None) are skipped, idle ones (no bids) too unless `drop_idle` is False; None when
    nothing is left."""
    kept = [s for s in stats if s is not None and (s["bids"] > 0 or not drop_idle)]
    if not kept:
        return None
    bids = sum(s["bids"] for s in kept)
    decided = sum(s["passed"] + s["rejected"] for s in kept)
    return {
        "profiles": len(kept),
        "bids": bids,
        "decided": decided,
        "bidsPerWeek": _mean([s["bids"] / s["weeks"] for s in kept]),
        "interviewsPerWeek": _mean([s["interviews"] / s["weeks"] for s in kept]),
        "tailoredResumesPerWeek": _mean([s["tailored"] / s["weeks"] for s in kept]),
        "interviewsPerBid": round(sum(s["interviews"] for s in kept) / bids, 4) if bids else None,
        "passRate": rate(decided, sum(s["passed"] for s in kept)),
        "tailorScore": _score([v for s in kept for v in s["tailorScores"]]),
        "matchScore": _score([v for s in kept for v in s["matchScores"]]),
    }
