"""Statistics → Callers: how many interviews each caller did and how often they pass, for a week, month or custom
range. See docs/superpowers/specs/2026-11-06-callers-statistics-design.md.

Rounds come from app/interview_rounds.merged_rounds (consecutive entries of one stage are one round), held on the app's
local day of their scheduledAt, in the region of the interview's profile. AI interviews, home assessments and stages
with no group don't count. A caller round has an enabled caller (an external caller or a platform user): its caller is caller.callerId, else the caller whose
name was typed (rounds saved before the caller list), else Unassigned. Every other round is a self round.
- interviews done = passed + rejected + awaiting (held, no result yet); canceled and still-scheduled rounds aren't done;
- pass rate = passed / (passed + rejected); a caller is ranked from MIN_DECIDED decided rounds;
- vs team = the caller's pass rate minus every caller round's pass rate.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.caller_directory import CallerEntry, list_directory
from app.interview_rates import Period, Region, _profile_regions, buckets, label, rate, unit, window

RESULTS = ("passed", "rejected", "awaiting", "canceled", "scheduled")
GROUPS = ("intro", "tech", "hiring", "panel", "final")
MIN_DECIDED = 5  # fewer decided rounds than this: the pass rate is shown but not ranked
# Other spellings typed for a caller: typed key → the caller's nameKey.
ALIASES = {"mohammad": "muhammad", "mohammed": "muhammad", "muhammed": "muhammad"}


def name_key(name: str) -> str:
    return " ".join((name or "").split()).lower()


def typed_caller(name: str, callers: dict):
    """The caller (`callers`: nameKey → caller) a typed caller name means, or None (TBD, unknown names)."""
    key = name_key(name)
    return callers.get(ALIASES.get(key, key))


def _caller_id(caller, by_key: dict) -> Optional[str]:
    """The id a caller round counts for: its callerId, else the caller whose name was typed, else None."""
    if caller.callerId:
        return caller.callerId
    typed = typed_caller(caller.callerName, by_key)
    return str(typed.id) if typed else None


def _utc(v: datetime) -> datetime:
    """Aware UTC: Mongo hands back naive UTC datetimes."""
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _iso(v: datetime) -> str:
    return _utc(v).isoformat().replace("+00:00", "Z")


def round_result(status: Optional[str]) -> str:
    """passed | rejected | canceled | awaiting (held, no result yet) | scheduled (or no status)."""
    if status in ("passed", "rejected", "canceled"):
        return status
    return "awaiting" if status == "completed" else "scheduled"


def _pass_rate(passed: int, rejected: int) -> Optional[float]:
    return rate(passed + rejected, passed)


def summary(rounds: list[dict]) -> dict:
    """{counts, done, passRate, byType} of these rounds (each has `result` and `group`)."""
    counts = {k: sum(1 for r in rounds if r["result"] == k) for k in RESULTS}
    by_type = {}
    for g in GROUPS:
        mine = [r for r in rounds if r["group"] == g]
        passed = sum(1 for r in mine if r["result"] == "passed")
        rejected = sum(1 for r in mine if r["result"] == "rejected")
        done = passed + rejected + sum(1 for r in mine if r["result"] == "awaiting")
        by_type[g] = {"done": done, "passed": passed, "rejected": rejected, "passRate": _pass_rate(passed, rejected)}
    return {"counts": counts, "done": counts["passed"] + counts["rejected"] + counts["awaiting"],
            "passRate": _pass_rate(counts["passed"], counts["rejected"]), "byType": by_type}


def _brief(rounds: list[dict]) -> dict:
    s = summary(rounds)
    return {"done": s["done"], "passRate": s["passRate"]}


async def caller_stats(period: Period, day: date, region: Region, until: Optional[date] = None,
                       now: Optional[datetime] = None) -> dict:
    from app.dashboard_stats import app_tz, local_day_start_utc
    from app.interview_rounds import _ROUND_COUNT_EXCLUDED, _breakdown_group, merged_rounds
    from app.models import Interview, UserExtra
    from app.models.caller import Caller

    tz = app_tz()
    start, end = window(period, day, until)
    first = local_day_start_utc(start, tz)
    last = local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    regions, _ = await _profile_regions()
    directory = await list_directory(include_archived=True)
    by_key = {c.nameKey: c for c in await Caller.find_all().to_list()}

    caller_rounds, self_rounds = [], []
    ivs = await Interview.find({"$or": [{"scheduledAt": {"$gte": first, "$lte": last}},
                                        {"stageHistory.scheduledAt": {"$gte": first, "$lte": last}}]}).to_list()
    for iv in ivs:
        if region != "all" and regions.get(iv.accountId) != region:
            continue
        for rd in merged_rounds(iv):
            group = _breakdown_group(rd.stage)
            if rd.at is None or group is None or rd.stage in _ROUND_COUNT_EXCLUDED:
                continue
            held = _utc(rd.at)
            held_day = held.astimezone(tz).date()
            if not start <= held_day <= end:
                continue
            row = {"result": round_result(rd.status), "group": group, "day": held_day}
            caller = rd.caller
            if caller is None or not getattr(caller, "enabled", False):
                self_rounds.append(row)
                continue
            row.update({
                "interviewId": str(iv.id), "roundId": rd.entry_id,
                "callerId": _caller_id(caller, by_key),
                "callerName": (caller.callerName or "").strip(), "roundDate": _iso(held),
                "company": (iv.companyName or "").strip(), "stage": rd.stage, "engineerId": iv.createdBy,
            })
            caller_rounds.append(row)

    team = summary(caller_rounds)
    rows = _caller_rows(directory, caller_rounds, team["passRate"])
    chart_unit = unit(period, start, end)
    weekly = []
    for s, e, lbl in buckets(chart_unit, start, end):
        inside = [r for r in caller_rounds if s <= r["day"] <= e]
        per: dict = defaultdict(list)
        for r in inside:
            per[r["callerId"] or "unassigned"].append(r)
        weekly.append({"label": lbl, "start": s.isoformat(), "end": e.isoformat(), "all": _brief(inside),
                       "callers": {k: _brief(v) for k, v in per.items()}})

    shown = [r for r in caller_rounds if r["result"] != "scheduled"]
    owners = {r["engineerId"] for r in shown}
    users = {u.id: u for u in await UserExtra.find({"_id": {"$in": list(owners)}}).to_list()} if owners else {}
    out_rounds = []
    for r in sorted(shown, key=lambda r: r["roundDate"]):
        u = users.get(r["engineerId"])
        out_rounds.append({
            "interviewId": r["interviewId"], "roundId": r["roundId"], "callerId": r["callerId"],
            "roundDate": r["roundDate"], "company": r["company"], "stage": r["stage"],
            "engineer": ((u.name or u.email) if u else "") or "", "result": r["result"],
        })

    return {
        "period": period, "region": region, "start": start.isoformat(), "end": end.isoformat(),
        "label": label(period, start, end),
        "callers": rows, "team": team, "self": summary(self_rounds),
        "weekly": {"unit": chart_unit, "buckets": weekly},
        "rounds": out_rounds,
    }


def _caller_rows(directory: list[CallerEntry], rounds: list[dict], team_rate: Optional[float]) -> list[dict]:
    """Every entry of the caller directory (platform users and external callers, archived ones too, even with no
    rounds), then callers no longer in it that have rounds, then Unassigned when it has any. Rounds of a caller no
    longer in the directory keep its id and typed name."""
    known = {e["_id"] for e in directory}
    rows = [{"callerId": e["_id"], "name": e["name"], "color": e["color"]} for e in directory]
    for r in rounds:
        cid = r["callerId"]
        if cid and cid not in known:
            known.add(cid)
            rows.append({"callerId": cid, "name": r["callerName"] or "Removed caller", "color": None})
    if any(r["callerId"] is None for r in rounds):
        rows.append({"callerId": None, "name": "Unassigned", "color": None})
    for row in rows:
        s = summary([r for r in rounds if r["callerId"] == row["callerId"]])
        row.update(s)
        row["ranked"] = s["counts"]["passed"] + s["counts"]["rejected"] >= MIN_DECIDED
        row["vsTeam"] = round(s["passRate"] - team_rate, 4) if s["passRate"] is not None and team_rate is not None else None
    return rows


OVERVIEW_DAYS = 90  # the overview's pass rate looks back this far


async def caller_overview(now: datetime) -> dict[str, dict]:
    """Per caller id, for the Callers tab: {upcoming, thisWeek, passRate, rounds}. Counts rounds like caller_stats
    does. upcoming = scheduled rounds starting after `now`; thisWeek = rounds (not canceled) on a day of the app-time
    Monday-Sunday week of `now`; passRate = passed / (passed + rejected) over the 90 days before `now`; rounds = every
    round the caller has had or has coming (any status). Callers with nothing are left out. Unassigned isn't listed."""
    from app.dashboard_stats import app_tz
    from app.interview_rounds import _ROUND_COUNT_EXCLUDED, _breakdown_group, merged_rounds
    from app.models import Interview
    from app.models.caller import Caller

    tz = app_tz()
    now = _utc(now)
    today = now.astimezone(tz).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    since = now - timedelta(days=OVERVIEW_DAYS)
    by_key = {c.nameKey: c for c in await Caller.find_all().to_list()}
    stats: dict[str, dict] = defaultdict(lambda: {"upcoming": 0, "thisWeek": 0, "passed": 0, "rejected": 0, "rounds": 0})

    ivs = await Interview.find({"$or": [{"stageHistory.caller.enabled": True}, {"caller.enabled": True}]}).to_list()
    for iv in ivs:
        for rd in merged_rounds(iv):
            if rd.at is None or _breakdown_group(rd.stage) is None or rd.stage in _ROUND_COUNT_EXCLUDED:
                continue
            caller = rd.caller
            if caller is None or not getattr(caller, "enabled", False):
                continue
            cid = _caller_id(caller, by_key)
            if cid is None:
                continue
            held = _utc(rd.at)
            result = round_result(rd.status)
            st = stats[cid]
            st["rounds"] += 1
            if result == "scheduled" and held > now:
                st["upcoming"] += 1
            if result != "canceled" and monday <= held.astimezone(tz).date() <= sunday:
                st["thisWeek"] += 1
            if result in ("passed", "rejected") and since <= held <= now:
                st[result] += 1

    return {
        cid: {"upcoming": st["upcoming"], "thisWeek": st["thisWeek"],
              "passRate": _pass_rate(st["passed"], st["rejected"]), "rounds": st["rounds"]}
        for cid, st in stats.items()
    }
