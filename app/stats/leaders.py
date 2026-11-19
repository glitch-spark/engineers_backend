"""Winning vs weakest profiles: the team's top 3 and bottom 3 by matured per-100, and what each group looks like."""
from collections import Counter
from statistics import median_low
from typing import Optional

from app.stats.job_fit import BOARD_LABELS

MIN_MATURED = 150
PICK = 3
DAYS_MIN = 10
_FIELDS = ("accountId", "name", "owner", "region", "matured", "interviews", "per100")


def _pct(n: int, d: int) -> Optional[int]:
    return round(n / d * 100) if d else None


def _top_shares(values: list) -> list[dict]:
    c = Counter(values)
    return [{"label": k, "share": _pct(n, len(values))} for k, n in sorted(c.items(), key=lambda x: (-x[1], x[0]))[:3]]


def _median(xs: list) -> Optional[int]:
    return median_low(xs) if xs else None


def _group(profiles: list[dict]) -> dict:
    rows = [r for p in profiles for r in p["rows"]]
    scores = [r["score"] for r in rows if r.get("score") is not None]
    areas = [r["area"] for r in rows if r.get("area")]
    days = [r["daysToInterview"] for r in rows if r.get("interviewId") and r.get("daysToInterview") is not None]
    return {
        "matured": sum(p["matured"] for p in profiles), "interviews": sum(p["interviews"] for p in profiles),
        "regions": dict(Counter(p["region"] for p in profiles)),
        "avgScore": round(sum(scores) / len(scores), 1) if scores else None, "scored": len(scores),
        "areas": _top_shares(areas), "areaKnown": _pct(len(areas), len(rows)),
        "medianProfileAge": _median([r["profileAgeDays"] for r in rows if r.get("profileAgeDays") is not None]),
        "sites": _top_shares([BOARD_LABELS.get(r["board"], r["board"]) for r in rows if r.get("board")]),
        "medianDays": _median(days), "interviewCount": len(days), "daysTooFew": len(days) < DAYS_MIN,
        "selfShare": _pct(sum(1 for r in rows if r.get("by") == "self"), len(rows)),
    }


def pick(team_profiles: list[dict], min_matured: int = MIN_MATURED) -> dict:
    """team_profiles: {accountId, name, owner, region, matured, interviews, per100, rows (non-rejected cohort rows)}."""
    ok = [p for p in team_profiles if p["matured"] >= min_matured]
    ranked = sorted(ok, key=lambda p: (-p["per100"], -p["matured"]))
    best = ranked[:PICK]
    weakest = sorted(ranked[PICK:], key=lambda p: (p["per100"], -p["matured"]))[:PICK]
    return {"best": [{k: p[k] for k in _FIELDS} for p in best], "weakest": [{k: p[k] for k in _FIELDS} for p in weakest],
            "groups": {"best": _group(best), "weakest": _group(weakest)}}
