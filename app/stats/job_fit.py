"""Interviews per 100 matured bids by job site, resume score, area and profile age (pure, no DB)."""
from datetime import datetime
from typing import Callable, Optional

from app.stats.profile_report import MIN_MATURED, _per100

BOARD_LABELS = {
    "ashbyhq.com": "Ashby", "greenhouse.io": "Greenhouse", "myworkdayjobs.com": "Workday", "workable.com": "Workable",
    "lever.co": "Lever", "bamboohr.com": "BambooHR", "rippling.com": "Rippling", "applytojob.com": "JazzHR",
    "smartrecruiters.com": "SmartRecruiters", "breezy.hr": "Breezy", "oraclecloud.com": "Oracle",
    "zohorecruit.com": "Zoho Recruit", "icims.com": "iCIMS", "teamtailor.com": "Teamtailor", "linkedin.com": "LinkedIn",
}
TOP_SITES = 8
_SCORES = [("strong", "80+"), ("good", "65–79"), ("fair", "50–64"), ("weak", "Under 50"), (None, "No tailored resume")]
_AREAS = ["Backend", "Full stack", "Frontend", "AI / ML", "Data", "DevOps", "QA", "Mobile", "Other", None]
_AGES = [("<2w", "Under 2 weeks"), ("2-6w", "2–6 weeks"), ("6w-3m", "6 weeks – 3 months"), ("3m+", "3+ months"),
         (None, "Unknown")]


def _age(days: Optional[int]) -> Optional[str]:
    if days is None:
        return None
    return "<2w" if days < 14 else "2-6w" if days < 42 else "6w-3m" if days < 91 else "3m+"


def _stats(rows: list[dict], matured_before: datetime) -> dict:
    matured = [r for r in rows if r["appliedAt"] <= matured_before]
    iv = sum(1 for r in matured if r.get("interviewId"))
    return {"bids": len(rows), "matured": len(matured), "interviews": iv, "per100": _per100(iv, len(matured)),
            "tooFew": len(matured) < MIN_MATURED}


def _groups(you: list[dict], team: list[dict], matured_before: datetime, keyf: Callable, order: list[tuple],
            skip_empty: bool = False) -> list[dict]:
    out = []
    for key, label in order:
        y, t = [r for r in you if keyf(r) == key], [r for r in team if keyf(r) == key]
        if skip_empty and not y and not t:
            continue
        out.append({"key": key, "label": label, "you": _stats(y, matured_before), "team": _stats(t, matured_before)})
    return out


def _share(rows: list[dict], field: str) -> Optional[int]:
    return round(sum(1 for r in rows if r.get(field) is not None) / len(rows) * 100) if rows else None


def breakdown(you: list[dict], team: list[dict], matured_before: datetime) -> dict:
    you = [r for r in you if r.get("bidStatus") != "rejected"]
    team = [r for r in team if r.get("bidStatus") != "rejected"]
    counts: dict[str, int] = {}
    for r in team:
        if r.get("board") and r["appliedAt"] <= matured_before:
            counts[r["board"]] = counts.get(r["board"], 0) + 1
    # ties on matured rows fall back to the domain so the order is stable; boards with no matured team rows are "Other"
    top = sorted(counts, key=lambda b: (-counts[b], b))[:TOP_SITES]

    def site(r: dict) -> Optional[str]:
        b = r.get("board")
        return "none" if not b else b if b in top else "other"

    sites = [(b, BOARD_LABELS.get(b, b)) for b in top] + [("other", "Other sites"), ("none", "No job link")]
    return {
        "bySite": _groups(you, team, matured_before, site, sites),
        "byScore": _groups(you, team, matured_before, lambda r: r.get("scoreBand"), _SCORES),
        "byArea": _groups(you, team, matured_before, lambda r: r.get("area"),
                          [(a, a or "Unknown") for a in _AREAS], skip_empty=True),
        "byAge": _groups(you, team, matured_before, lambda r: _age(r.get("profileAgeDays")), _AGES),
        "known": {"score": _share(you, "scoreBand"), "area": _share(you, "area")},
    }
