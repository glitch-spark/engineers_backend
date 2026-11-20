"""Interviews statistics key findings and the ★ / ⚠ group marks (pure). Rules and copy: the 2026-11-20 interview
statistics spec, "Findings". `team_rate` is the unrounded team pass rate (interview_report.pass_rate)."""
from math import comb
from typing import Optional

from app.stats import conversion_findings
from app.stats.conversion_findings import CATCH_ALLS, FALLBACK, MAX, _pct

BY = {"byStage": "stage", "byWho": "who took the round", "byCaller": "caller", "byProfile": "profile",
      "byRegion": "region", "byArea": "job area", "byScore": "resume score", "byAppliedBy": "who applied",
      "byWeekday": "weekday", "byHour": "time of day"}
UNNAMED_REASONS = {"none", "no_reason", "other"}
TOP_REASON = (40, 10)  # a reason's share (%) of at least this many rejected / canceled rounds
NO_ANSWER = (30, 20)  # no answer's share (%) of at least this many decided rounds


def _tail(lo: int, hi: int, n: int, p: float) -> float:
    """Binomial P(lo <= X <= hi | n, p)."""
    return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(lo, hi + 1))


def _rate(g: dict) -> float:
    return g["passed"] / g["decided"] * 100


def _picks(report: dict, team_rate: Optional[float]) -> dict:
    """Per breakdown: (best group or None, weak group or None). Best = the highest rate among the non-few groups at
    >= 1.5 x the team rate with P(X >= passed | decided, team rate) <= 0.05; weak = the lowest at <= 0.5 x with
    P(X <= passed) <= 0.05. Catch-alls ("unknown", "other", area Other) never qualify; no team rate → nothing."""
    out = {}
    p = (team_rate or 0) / 100
    for by in BY:
        groups = [g for g in report.get(by, []) if g["key"] not in CATCH_ALLS and not g["few"]] if team_rate else []
        best = [g for g in groups if g["passed"] * 100 >= 1.5 * team_rate * g["decided"]
                and _tail(g["passed"], g["decided"], g["decided"], p) <= 0.05]
        weak = [g for g in groups if g["passed"] * 100 <= 0.5 * team_rate * g["decided"]
                and _tail(0, g["passed"], g["decided"], p) <= 0.05]
        out[by] = (max(best, key=lambda g: (_rate(g), g["passed"])) if best else None,
                   min(weak, key=lambda g: (_rate(g), -g["decided"])) if weak else None)
    return out


def findings(report: dict, team_rate: Optional[float]) -> list[dict]:
    """One best and one weak line per breakdown, the top rejected / canceled reason and the no-answer line; at most 4,
    the one furthest past its own threshold first; none → one `kind: "none"` item with the fallback sentence."""
    out = []
    for by, (best, weak) in _picks(report, team_rate).items():
        for g, kind in ((best, "best"), (weak, "weak")):
            if not g:
                continue
            nums = f"{_pct(_rate(g))}% ({g['passed']:,} of {g['decided']:,} decided rounds) vs {_pct(team_rate)}% overall."
            if kind == "best":
                out.append((_rate(g) / team_rate / 1.5, {"kind": kind, "text": f"{g['label']} passes best for {BY[by]}: {nums}"}))
            else:
                out.append((0.5 * team_rate / _rate(g) if _rate(g) else float("inf"),
                            {"kind": kind, "text": f"{g['label']}: {nums.replace('% (', '% pass (', 1)}"}))
    for status in ("rejected", "canceled"):
        reasons = report["reasons"][status]
        total = sum(r["count"] for r in reasons)
        top = max((r for r in reasons if r["key"] not in UNNAMED_REASONS), key=lambda r: r["count"], default=None)
        if top and total >= TOP_REASON[1] and top["count"] * 100 >= TOP_REASON[0] * total:
            share = top["count"] / total * 100
            out.append((share / TOP_REASON[0], {"kind": "reason", "text": (
                f"{_pct(share)}% of {status} rounds: {top['label']} ({top['count']:,} of {total:,}).")}))
    t = report["totals"]
    if t["decided"] >= NO_ANSWER[1] and t["noAnswer"] * 100 >= NO_ANSWER[0] * t["decided"]:
        share = t["noAnswer"] / t["decided"] * 100
        out.append((share / NO_ANSWER[0], {"kind": "no_answer", "text": (
            f"{_pct(share)}% of held rounds got no answer ({t['noAnswer']:,} of {t['decided']:,}) — follow up after a week.")}))
    out.sort(key=lambda x: -x[0])  # stable: equal effects keep the order above
    return [f for _, f in out[:MAX]] or [{"kind": "none", "text": FALLBACK}]


def mark_groups(report: dict, team_rate: Optional[float]) -> None:
    """`mark` per table from the same rules, whatever the 4-line cap; folded members are never marked."""
    conversion_findings.mark_groups(report, team_rate, _picks(report, team_rate))
