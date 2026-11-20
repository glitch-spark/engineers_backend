"""Conversion tab key findings and the ★ / ⚠ group marks (pure). Rules and copy: the 2026-11-19 conversion redesign
spec, "Findings rules". `team_rate` is the unrounded per-100 team rate the report was built with."""
import math
from typing import Optional

from app.stats.profile_report import EXPECTED_MIN, MIN_MATURED

FALLBACK = "No clear pattern yet in this period — try a longer date range."
BY = {"byScore": "resume score", "bySite": "job site", "byArea": "job area", "byAge": "profile age",
      "byWho": "who applied"}
# No score / Unknown, No job link, Other sites, area Other, No tracked bid
CATCH_ALLS = {None, "none", "other", "Other", "unknown"}
MAX = 4


def _pct(x: float) -> str:
    return f"{x:.1f}"


def _rate(g: dict) -> float:
    return g["interviews"] / g["matured"] * 100


def _chance(i: int, expected: float) -> float:
    """Poisson upper tail P(X >= i | lambda = expected): how often a group this lucky happens by chance."""
    return 1 - sum(math.exp(-expected) * expected ** k / math.factorial(k) for k in range(i))


def _picks(report: dict, team_rate: Optional[float]) -> dict:
    """Per breakdown: (best group or None, weakest group or None) by the spec rules. Best = highest rate among
    non-few groups with >= 3 interviews and rate >= 1.5 x team and Poisson upper-tail P(X >= interviews | expected) <= 0.05; weak = largest expected (matured x team / 100 >= 3)
    among non-few groups with 0 interviews. Catch-alls never qualify; no team rate → nothing."""
    out = {}
    for by in BY:
        groups = [g for g in report.get(by, []) if g["key"] not in CATCH_ALLS and not g["few"]] if team_rate else []
        best = [g for g in groups if g["interviews"] >= EXPECTED_MIN
                and g["interviews"] * 100 >= 1.5 * team_rate * g["matured"]
                and _chance(g["interviews"], g["matured"] * team_rate / 100) <= 0.05]
        weak = [g for g in groups if g["interviews"] == 0 and g["matured"] * team_rate >= EXPECTED_MIN * 100]
        out[by] = (max(best, key=lambda g: (_rate(g), g["interviews"])) if best else None,
                   max(weak, key=lambda g: g["matured"]) if weak else None)
    return out


def _gap(report: dict) -> Optional[tuple]:
    """(effect, tie, finding) when your self bids convert at 2x+ your bidder bids, else None."""
    who = {g["key"]: g for g in report.get("byWho", [])}
    s, b = who.get("self"), who.get("bidder")
    if not (s and b and s["matured"] >= MIN_MATURED and b["matured"] >= MIN_MATURED
            and s["interviews"] >= EXPECTED_MIN and s["interviews"] * b["matured"] >= 2 * b["interviews"] * s["matured"]):
        return None
    text = (f"Your bidder bids convert at {_pct(_rate(b))}% vs your self bids {_pct(_rate(s))}% "
            f"({b['interviews']:,} of {b['matured']:,} vs {s['interviews']:,} of {s['matured']:,}).")
    side = lambda g: {"interviews": g["interviews"], "matured": g["matured"], "rate": round(_rate(g), 1)}  # noqa: E731
    return (_rate(s) / _rate(b) if _rate(b) else float("inf"), s["interviews"],
            {"kind": "gap", "text": text, "evidence": {"by": "byWho", "self": side(s), "bidder": side(b)}})


def findings(report: dict, team_rate: Optional[float], scope: str) -> list[dict]:
    """One best and one weak line per breakdown plus (scope me) the self vs bidder gap, biggest effect first, at most
    4; none → one `kind: "none"` item carrying the fallback sentence. Effects of different kinds share one scale on
    purpose: best = rate / team rate, gap = self rate / bidder rate, weak = expected interviews."""
    out = []
    gap = _gap(report) if scope == "me" else None
    if gap:
        out.append(gap)
    for by, (best, weak) in _picks(report, team_rate).items():
        if gap and by == "byWho":  # the gap line already says Self vs Bidder
            continue
        ev = lambda g: {"by": by, "key": g["key"], "label": g["label"], "rate": g["rate"],  # noqa: E731
                        "interviews": g["interviews"], "matured": g["matured"], "teamRate": round(team_rate, 1)}
        if best:
            g = best
            out.append((_rate(g) / team_rate, g["interviews"], {"kind": "best", "evidence": ev(g), "text": (
                f"{g['label']} converts best for {BY[by]}: {_pct(_rate(g))}% ({g['interviews']:,} interviews from "
                f"{g['matured']:,} matured bids) vs {_pct(team_rate)}% across all matured bids.")}))
        if weak:
            g, e = weak, weak["matured"] * team_rate / 100
            out.append((e, e, {"kind": "weak", "evidence": {**ev(g), "expected": round(e, 1)}, "text": (
                f"{g['label']}: 0 interviews from {g['matured']:,} matured bids — the team rate would give "
                f"about {int(e + 0.5):,}.")}))
    out.sort(key=lambda t: (-t[0], -t[1]))
    return [f for _, _, f in out[:MAX]] or [{"kind": "none", "text": FALLBACK, "evidence": {}}]


def mark_groups(report: dict, team_rate: Optional[float]) -> None:
    """`mark` per breakdown table from the same rules, independent of the 4-line cap and the gap line: "best" on its
    best group, "weak" on its weakest; everything else None. Other sites' members are never marked."""
    for by, (best, weak) in _picks(report, team_rate).items():
        for g in report.get(by, []):
            g["mark"] = "best" if g is best else "weak" if g is weak else None
