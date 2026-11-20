"""Conversion tab report: interviews per matured bid, in total and by bidder / site / score / area / profile age
(pure, no DB). Rows are the scope's loader rows; rejected ones are dropped here."""
from datetime import datetime
from typing import Callable, Optional

from app.stats.job_fit import _AGES, _AREAS, _SCORES, BOARD_LABELS, TOP_SITES, _age
from app.stats.profile_report import EXPECTED_MIN, MIN_MATURED


def _rate(iv: int, matured: int) -> Optional[float]:
    return round(iv / matured * 100, 1) if matured else None


def _few(matured: int, interviews: int, team_rate: Optional[float]) -> Optional[str]:
    """Why a rate is too thin to read: under 30 matured bids, or under 3 interviews behind it while the team rate
    would also expect under 3 (changed 2026-11-19 after real data); None when it's fine."""
    if matured < MIN_MATURED:
        return "matured"
    if interviews < EXPECTED_MIN and team_rate and matured * team_rate / 100 < EXPECTED_MIN:
        return "expected"
    return None


def _counts(rows: list[dict], matured_before: datetime, team_rate: Optional[float] = None) -> dict:
    matured = [r for r in rows if r["appliedAt"] <= matured_before]
    iv = sum(1 for r in matured if r.get("interviewId"))
    why = _few(len(matured), iv, team_rate)
    return {"bids": len(rows), "matured": len(matured), "interviews": iv, "rate": _rate(iv, len(matured)),
            "few": why is not None, "fewReason": why}


def _groups(rows: list[dict], matured_before: datetime, team_rate: Optional[float], keyf: Callable,
            order: list[tuple], skip_empty: bool = False) -> list[dict]:
    out = []
    for key, label in order:
        mine = [r for r in rows if keyf(r) == key]
        if not (skip_empty and not mine):
            out.append({"key": key, "label": label, **_counts(mine, matured_before, team_rate)})
    return out


def report(rows: list[dict], matured_before: datetime, bidder_names: dict, include_bidders: bool,
           team_rate: Optional[float] = None) -> dict:
    """`team_rate`: the team rows' pooled matured rate (unrounded, as grades use), for the `few` expected rule."""
    rows = [r for r in rows if r.get("bidStatus") != "rejected"]
    t = _counts(rows, matured_before)
    all_iv = sum(1 for r in rows if r.get("interviewId"))
    out: dict = {"totals": {"bids": t["bids"], "matured": t["matured"], "interviews": t["interviews"],
                            "interviewsAll": all_iv,
                            "interviewOnly": sum(1 for r in rows if r.get("interviewId") and r.get("byInterviewOnly")),
                            "inPlay": t["bids"] - t["matured"], "rate": t["rate"]},
                 "teamRate": round(team_rate, 1) if team_rate is not None else None}
    if include_bidders:
        def who(r: dict) -> str:
            if r.get("by") in ("self", "unknown"):
                return r["by"]
            return str(r["bidderId"]) if r.get("bidderId") in bidder_names else "unnamed"

        by_key = {str(k): n for k, n in bidder_names.items()}
        keys = {who(r) for r in rows} - {"self", "unknown"}
        groups = _groups(rows, matured_before, team_rate, who, [(k, by_key.get(k, "Unnamed bidder")) for k in keys])
        groups.sort(key=lambda g: (-g["bids"], g["label"]))
        out["byBidder"] = _groups(rows, matured_before, team_rate, who, [("self", "Self")], skip_empty=True) + _groups(
            rows, matured_before, team_rate, who, [("unknown", "No tracked bid")], skip_empty=True) + groups
    # self vs bidder bids, every scope (no names, so Team total shows it too) — user, 2026-11-19
    by = lambda r: r.get("by") or "self"  # noqa: E731
    out["byWho"] = _groups(rows, matured_before, team_rate, by, [("self", "Self"), ("bidder", "Bidder")]) + _groups(
        rows, matured_before, team_rate, by, [("unknown", "No tracked bid")], skip_empty=True)
    counts: dict[str, int] = {}
    for r in rows:
        if r.get("board"):
            counts[r["board"]] = counts.get(r["board"], 0) + 1
    top = sorted(counts, key=lambda b: (-counts[b], b))[:TOP_SITES]

    def site(r: dict) -> str:
        b = r.get("board")
        return "none" if not b else b if b in top else "other"

    sites = [(b, BOARD_LABELS.get(b, b)) for b in top] + [("other", "Other sites"), ("none", "No job link")]
    out["bySite"] = _groups(rows, matured_before, team_rate, site, sites)
    by_board: dict[str, list[dict]] = {}
    for r in rows:  # one pass: ~700 boards x all rows via _groups would be quadratic
        if r.get("board") and r["board"] not in top:
            by_board.setdefault(r["board"], []).append(r)
    members = sorted(({"key": b, "label": BOARD_LABELS.get(b, b), **_counts(rs, matured_before, team_rate)}
                      for b, rs in by_board.items()), key=lambda g: (-g["bids"], g["label"]))
    other = next(g for g in out["bySite"] if g["key"] == "other")
    other.update(members=members[:50], moreMembers=max(0, len(members) - 50), memberCount=len(members))
    out["byScore"] = _groups(rows, matured_before, team_rate, lambda r: r.get("scoreBand"), _SCORES)
    out["byArea"] = _groups(rows, matured_before, team_rate, lambda r: r.get("area"),
                            [(a, a or "Unknown") for a in _AREAS], skip_empty=True)
    out["byAge"] = _groups(rows, matured_before, team_rate, lambda r: _age(r.get("profileAgeDays")), _AGES)
    return out
