"""Statistics → Callers: the subject as a caller (when they called a round in the period) or as an engineer whose
rounds callers took, against the team (see docs/superpowers/specs/2026-11-18-stats-insights-design.md)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from app import interview_rates
from app.caller_stats import GROUPS, MIN_DECIDED, _caller_rows, collect_rounds, list_directory, summary
from app.stats import frame
from app.stats.interviews import _GROUP_LABEL, _rate, caller_vs_self

CALLER_GAP = 0.15
BACKLOG_ROUNDS = 5  # more awaiting rounds than this ...
BACKLOG_DAYS = 7  # ... held longer ago than this is a backlog

DEFINITIONS = [
    "A held caller round is a round in the period with an external caller or a platform user assigned (canceled rounds, AI interviews and home assessments are not counted), in the interview's profile region under a region filter.",
    "Pass rate is passed ÷ (passed + rejected); rounds still awaiting a result are left out.",
    f"A caller is ranked from {MIN_DECIDED} decided rounds; Unassigned rounds count for the team but are never ranked.",
]


def _tip(id: str, severity: str, text: str, detail: str, evidence: dict, impact: float) -> dict:
    return frame.tip(f"callers.{id}", severity, text, detail, evidence, impact)


def _pr(rounds: list[dict]) -> tuple[int, int]:
    return (sum(r["result"] == "passed" for r in rounds), sum(r["result"] == "rejected" for r in rounds))


def _done(rounds: list[dict]) -> int:
    return sum(r["result"] in ("passed", "rejected", "awaiting") for r in rounds)


def weak_round_type(mine: dict, team: dict) -> Optional[dict]:
    """`mine` / `team`: group → (passed, rejected). The worst group (≥ MIN_DECIDED decided on both sides) where the
    subject passes more than CALLER_GAP under the team."""
    best = None
    for group, (p, r) in mine.items():
        tp, tr = team.get(group, (0, 0))
        if p + r < MIN_DECIDED or tp + tr < MIN_DECIDED:
            continue
        me, them = _rate(p, r), _rate(tp, tr)
        if me < them - CALLER_GAP and (best is None or them - me > best[0]):
            best = (them - me, group, me, them, p + r)
    if not best:
        return None
    gap, group, me, them, decided = best
    label = _GROUP_LABEL.get(group, group)
    return _tip("weak_round_type", "bad", f"{label}: {me:.0%} pass vs {them:.0%} for other callers",
                f"{decided} decided {label.lower()}; this is your weakest round type.",
                {"group": group, "myPassRate": me, "teamPassRate": them, "decided": decided}, gap * decided)


def awaiting_backlog(rounds: list[dict], today: date) -> Optional[dict]:
    n = sum(r["result"] == "awaiting" and (today - r["day"]).days > BACKLOG_DAYS for r in rounds)
    if n <= BACKLOG_ROUNDS:
        return None
    return _tip("awaiting_backlog", "watch", f"{n} rounds are still waiting for a result after a week — update them",
                "Rounds held more than 7 days ago with no result recorded.", {"awaiting": n}, n)


def no_rounds() -> dict:
    return _tip("no_rounds", "watch", "No interview rounds this period.",
                "Rounds you or a caller take on your interviews show up here.", {}, 0)


def no_caller_rounds(self_decided: int, caller_rounds: int) -> Optional[dict]:
    if caller_rounds or self_decided < MIN_DECIDED:
        return None
    return _tip("no_caller_rounds", "watch", "No caller rounds on your interviews this period",
                f"You took {self_decided} rounds yourself with a result.", {"selfDecided": self_decided}, 0)


def _row(key: str, label: str, value, fmt: str, team: Optional[dict], rank, of, hib: bool, note: str, eligible: bool = True) -> dict:
    return {"key": key, "label": label, "value": value, "format": fmt, "team": team, "rank": rank, "of": of,
            "vsMedian": frame.vs_median(value, team["median"]) if team and value is not None and eligible else None,
            "higherIsBetter": hib, "note": note}


def _rank(value, values: list, hib: bool, in_team: bool, eligible: bool = True):
    return frame.rank_of(value, values, hib, in_team) if eligible and value is not None else (None, len(values))


async def callers_view(subject, period: str, day: date, region: str, until: Optional[date], today: date) -> dict:
    from app.routers.metrics import _display_name

    start, end = interview_rates.window(period, day, until)
    caller_rounds, self_rounds = await collect_rounds(start, end, region)
    sid = str(subject.id)
    by_caller: dict = defaultdict(list)
    for r in caller_rounds:
        by_caller[r["callerId"]].append(r)
    mode = "caller" if _done(by_caller.get(sid, [])) else "engineer"  # a held round: scheduled / canceled don't count

    team_summary = summary(caller_rounds)
    rows = _caller_rows(await list_directory(include_archived=True), caller_rounds, team_summary["passRate"])
    rows = [r for r in rows if r["callerId"] in by_caller]
    ranked = [r for r in rows if r["ranked"] and r["callerId"] is not None]
    rates = [r["passRate"] for r in ranked]
    rate_d = frame.distribution(rates)
    ev_rows = []
    for r in rows:
        cid = r["callerId"]
        p, rej = r["counts"]["passed"], r["counts"]["rejected"]
        is_ranked = r["ranked"] and cid is not None
        ev_rows.append({
            "userId": cid or "unassigned", "name": r["name"], "value": r["passRate"],
            "vsMedian": frame.vs_median(r["passRate"], rate_d["median"]) if is_ranked else None,
            "rank": frame.rank_of(r["passRate"], rates, True, True)[0] if is_ranked else None,
            "parts": {"passed": p, "rejected": rej}, "isSubject": (cid or "unassigned") == sid})
    ev_rows.sort(key=lambda r: (r["rank"] is None, -(r["value"] or 0) if r["rank"] else -sum(r["parts"].values()), r["name"].lower()))

    ps, pe = frame.previous_window(period, start, end, today)
    tips: list[dict] = []
    if mode == "caller":
        mine = by_caller[sid]
        s = summary(mine)
        p, rej = s["counts"]["passed"], s["counts"]["rejected"]
        eligible = p + rej >= MIN_DECIDED
        done_by = {cid: _done(rs) for cid, rs in by_caller.items() if cid is not None}
        done_team = [d for d in done_by.values() if d >= 1]
        done_d = frame.distribution(done_team)
        types = [(g, s["byType"][g]["passRate"]) for g in GROUPS
                 if s["byType"][g]["passed"] + s["byType"][g]["rejected"] >= MIN_DECIDED]
        weak = min(types, key=lambda x: x[1]) if len(types) >= 2 else None
        rk, of = _rank(s["passRate"], rates, True, eligible, eligible)
        d_rk, d_of = _rank(s["done"], done_team, True, s["done"] >= 1)
        headline = [
            _row("passRate", "Your pass rate as a caller", s["passRate"], "pct", rate_d, rk, of, True, f"{p} passed · {rej} rejected", eligible),
            {"key": "rank", "label": "Your rank", "value": rk, "format": "rank", "team": None, "rank": rk, "of": of,
             "vsMedian": None, "higherIsBetter": False, "note": f"of {of}" if eligible else f"needs {MIN_DECIDED} decided rounds"},
            _row("done", "Rounds done", s["done"], "count", done_d, d_rk, d_of, True, f"{s['counts']['awaiting']} awaiting a result"),
            {"key": "weakest", "label": "Weakest round type", "value": weak[1] if weak else None, "format": "pct", "team": None,
             "rank": None, "of": 0, "vsMedian": None, "higherIsBetter": True, "note": _GROUP_LABEL[weak[0]] if weak else ""},
        ]
        n_days = len(frame.working_days(start, end, today))
        not_enough = frame.not_enough_data(len(ranked), n_days)
        if not not_enough:
            by_group = lambda rs: {g: _pr([r for r in rs if r["group"] == g]) for g in GROUPS}  # noqa: E731
            today_end = min(end, today)
            so_far = _done([r for r in mine if r["day"] <= today_end])
            before = _done([r for r in (await collect_rounds(ps, pe, region))[0] if r["callerId"] == sid])
            tips = frame.order_tips([t for t in (
                weak_round_type(by_group(mine), by_group([r for r in caller_rounds if r["callerId"] not in (None, sid)])) if eligible else None,
                awaiting_backlog(mine, today),
                frame.up_vs_last_period("callers", "rounds done", so_far, before, frame.span_label(ps, pe)),
            ) if t])
        days: dict = defaultdict(lambda: [0])
        for cid, rs in by_caller.items():
            if cid is not None:
                for r in rs:
                    days[(r["day"], cid)][0] += r["result"] in ("passed", "rejected", "awaiting")
        trend = frame.trend(start, end, today, days, (0,), sid, [c for c, d in done_by.items() if d >= 1])
    else:
        from app.models import UserExtra

        admins = {u.id for u in await UserExtra.find({"role": "admin"}).to_list()}
        by_eng: dict = defaultdict(lambda: {"self": [], "caller": []})
        for kind, rs in (("self", self_rounds), ("caller", caller_rounds)):
            for r in rs:
                by_eng[r["engineerId"]][kind].append(r)
        sidx = subject.id
        mine = by_eng[sidx] if sidx in by_eng else {"self": [], "caller": []}
        self_team = {e: v["self"] for e, v in by_eng.items() if e not in admins and sum(_pr(v["self"])) >= MIN_DECIDED}
        self_rates = [_rate(*_pr(rs)) for rs in self_team.values()]
        caller_rates = [_rate(*_pr(v["caller"])) for e, v in by_eng.items() if e not in admins and sum(_pr(v["caller"])) >= 1]
        wait = {e: sum(r["result"] == "awaiting" for r in v["self"] + v["caller"]) for e, v in by_eng.items()
                if e not in admins and _done(v["self"] + v["caller"])}
        my_self, my_caller = _pr(mine["self"]), _pr(mine["caller"])
        self_rate, caller_rate = _rate(*my_self), _rate(*my_caller)
        sd, cd, wd = frame.distribution(self_rates), frame.distribution(caller_rates), frame.distribution(list(wait.values()))
        s_rk, s_of = _rank(self_rate, self_rates, True, sidx in self_team, sum(my_self) >= MIN_DECIDED)
        c_rk, c_of = _rank(caller_rate, caller_rates, True, sum(my_caller) >= 1 and sidx not in admins)
        my_wait = wait.get(sidx, 0)
        w_rk, w_of = frame.rank_of(my_wait, list(wait.values()), False, sidx in wait)
        headline = [
            _row("selfRate", "Pass rate when you take the round", self_rate, "pct", sd, s_rk, s_of, True,
                 f"{my_self[0]} passed · {my_self[1]} rejected", sum(my_self) >= MIN_DECIDED),
            {"key": "rank", "label": "Your rank", "value": s_rk, "format": "rank", "team": None, "rank": s_rk, "of": s_of,
             "vsMedian": None, "higherIsBetter": False,
             "note": f"of {s_of} among people who take their own rounds" if sum(my_self) >= MIN_DECIDED else f"needs {MIN_DECIDED} decided rounds"},
            _row("callerRate", "Caller rounds on your interviews", caller_rate, "pct", cd, c_rk, c_of, True,
                 f"{sum(my_caller)} decided rounds"),
            _row("awaiting", "Awaiting a result", my_wait, "count", wd, w_rk, w_of, False, ""),
        ]
        not_enough = frame.not_enough_data(max(len(self_team), len(caller_rates)), len(frame.working_days(start, end, today)))
        if not not_enough:
            tips = frame.order_tips([t for t in (
                caller_vs_self(my_caller, my_self),
                awaiting_backlog(mine["self"] + mine["caller"], today),
                no_caller_rounds(sum(my_self), _done(mine["caller"])),
                no_rounds() if not _done(mine["self"] + mine["caller"]) else None,
            ) if t])
        trend = None

    return {
        "tab": "callers", "mode": mode, "label": interview_rates.label(period, start, end),
        "subject": {"userId": sid, "name": _display_name(subject)},
        "headline": headline, "tips": tips, "everyone": {"rowKind": "caller", "metric": "passRate", "rows": ev_rows},
        "trend": trend, "notEnoughData": not_enough, "definitions": DEFINITIONS,
    }
