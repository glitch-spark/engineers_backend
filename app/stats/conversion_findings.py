"""Conversion tab key findings and the ★ / ⚠ group marks (pure). Rules and copy: the 2026-11-19 conversion redesign
spec, "Findings rules". `team_rate` is the unrounded per-100 team rate the report was built with."""
from typing import Optional

from app.stats.profile_report import EXPECTED_MIN, MIN_MATURED

FALLBACK = "No clear pattern yet in this period — try a longer date range."
BY = {"byScore": "resume score", "bySite": "job site", "byArea": "job area", "byAge": "profile age",
      "byWho": "who applied"}
CATCH_ALLS = {None, "none", "other", "unknown"}  # No score / Unknown, No job link, Other sites, No tracked bid
MAX = 4


def _pct(x: float) -> str:
    return f"{x:.1f}"


def _rate(g: dict) -> float:
    return g["interviews"] / g["matured"] * 100


def _gap(report: dict) -> Optional[tuple]:
    """(effect, tie, finding) when your self bids convert at 2x+ your bidder bids, else None."""
    who = {g["key"]: g for g in report.get("byWho", [])}
    s, b = who.get("self"), who.get("bidder")
    if not (s and b and s["matured"] >= MIN_MATURED and b["matured"] >= MIN_MATURED
            and s["interviews"] >= EXPECTED_MIN and _rate(s) >= 2 * _rate(b)):
        return None
    text = (f"Your bidder bids convert at {_pct(_rate(b))}% vs your self bids {_pct(_rate(s))}% "
            f"({b['interviews']} of {b['matured']} vs {s['interviews']} of {s['matured']}).")
    side = lambda g: {"interviews": g["interviews"], "matured": g["matured"], "rate": round(_rate(g), 1)}  # noqa: E731
    return (_rate(s) / _rate(b) if _rate(b) else float("inf"), s["interviews"],
            {"kind": "gap", "text": text, "evidence": {"by": "byWho", "self": side(s), "bidder": side(b)}})


def findings(report: dict, team_rate: Optional[float], scope: str) -> list[dict]:
    """Best group per breakdown, weak big groups and (scope me) the self vs bidder gap, biggest effect first, at most
    4; none → one `kind: "none"` item carrying the fallback sentence."""
    out = []
    gap = _gap(report) if scope == "me" else None
    if gap:
        out.append(gap)
    for by, name in BY.items():
        if not team_rate or (gap and by == "byWho"):  # the gap line already says Self vs Bidder
            continue
        groups = [g for g in report.get(by, []) if g["key"] not in CATCH_ALLS and not g["few"]]
        ev = lambda g: {"by": by, "key": g["key"], "label": g["label"], "rate": g["rate"],  # noqa: E731
                        "interviews": g["interviews"], "matured": g["matured"], "teamRate": round(team_rate, 1)}
        best = [g for g in groups if g["interviews"] >= EXPECTED_MIN and _rate(g) >= 1.5 * team_rate]
        if best:
            g = max(best, key=lambda g: (_rate(g), g["interviews"]))
            out.append((_rate(g) / team_rate, g["interviews"], {"kind": "best", "evidence": ev(g), "text": (
                f"{g['label']} converts best for {name}: {_pct(_rate(g))}% ({g['interviews']} interviews from "
                f"{g['matured']} matured bids) vs {_pct(team_rate)}% overall.")}))
        for g in groups:
            e = g["matured"] * team_rate / 100
            if e >= EXPECTED_MIN and g["interviews"] == 0:
                out.append((e, e, {"kind": "weak", "evidence": {**ev(g), "expected": round(e, 1)}, "text": (
                    f"{g['label']}: 0 interviews from {g['matured']} matured bids — the team rate would give "
                    f"about {round(e)}.")}))
    out.sort(key=lambda t: (-t[0], -t[1]))
    return [f for _, _, f in out[:MAX]] or [{"kind": "none", "text": FALLBACK, "evidence": {}}]


def mark_groups(report: dict, found: list[dict]) -> None:
    """`mark` = "best" / "weak" on exactly the groups the findings name (gap: Self best, Bidder weak); few groups and
    Other sites' members are never marked."""
    marks = {}
    for f in found:
        ev = f["evidence"]
        if f["kind"] == "gap":
            marks[("byWho", "self")], marks[("byWho", "bidder")] = "best", "weak"
        elif f["kind"] in ("best", "weak"):
            marks[(ev["by"], ev["key"])] = f["kind"]
    for by in BY:
        for g in report.get(by, []):
            g["mark"] = None if g["few"] else marks.get((by, g["key"]))
