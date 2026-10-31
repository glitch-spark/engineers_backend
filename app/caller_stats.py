"""Statistics → Callers: each caller's interviews for a week, month or custom range, which callers are paid by.
See docs/superpowers/specs/2026-10-30-caller-stats-design.md.

A caller round is a round with a caller requested; its caller is caller.callerId, else the caller whose name was typed
(rounds saved before the caller list), else Unassigned. A round counts in the period it was held, with its result as it
is now. Sections:
- period: rounds held within the period: passed, rejected, canceled or awaiting their result;
- outdated: rounds held before the period, still awaiting their result;
- scheduled: rounds still scheduled, dated on or before the period's last day (stale when the time has passed).
Pass rate = passed / (passed + rejected) in the period. Days are the app's local days; the region is the interview
profile's, as on the Interview rate tab.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.interview_rates import Period, Region, _profile_regions, label, rate, window

RESULTS = ("passed", "rejected", "canceled", "awaiting")  # a held round's result; awaiting = held, none yet
# Other spellings typed for a caller: typed key → the caller's nameKey.
ALIASES = {"mohammad": "muhammad", "mohammed": "muhammad", "muhammed": "muhammad"}


def name_key(name: str) -> str:
    return " ".join((name or "").split()).lower()


def typed_caller(name: str, callers: dict):
    """The caller (`callers`: nameKey → caller) a typed caller name means, or None (TBD, unknown names)."""
    key = name_key(name)
    return callers.get(ALIASES.get(key, key))


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


def placed(result: str, held: date, start: date, end: date) -> Optional[str]:
    """The section a round held on the local day `held` goes in for the period [start, end], or None."""
    if held > end:
        return None
    if result == "scheduled":
        return "scheduled"
    if held >= start:
        return "period"
    return "outdated" if result == "awaiting" else None


async def caller_stats(period: Period, day: date, region: Region, until: Optional[date] = None,
                       now: Optional[datetime] = None) -> dict:
    from app.dashboard_stats import app_tz
    from app.models import Interview, UserExtra
    from app.models.caller import Caller

    tz = app_tz()
    now = _utc(now or datetime.now(timezone.utc))
    today = now.astimezone(tz).date()
    start, end = window(period, day, until)
    regions, _ = await _profile_regions()
    callers = sorted(await Caller.find_all().to_list(), key=lambda c: c.nameKey)
    by_key = {c.nameKey: c for c in callers}

    rounds, owners = [], set()
    for iv in await Interview.find({"stageHistory.caller.enabled": True}).to_list():
        if region != "all" and regions.get(iv.accountId) != region:
            continue
        for e in iv.stageHistory:
            if e.caller is None or not e.caller.enabled or (held := e.scheduledAt or e.at) is None:
                continue
            result = round_result(getattr(e.status, "value", e.status))
            held_day = _utc(held).astimezone(tz).date()
            section = placed(result, held_day, start, end)
            if section is None:
                continue
            ends = e.endsAt or held + timedelta(hours=1)
            typed = None if e.caller.callerId else typed_caller(e.caller.callerName, by_key)
            rounds.append({
                "interviewId": str(iv.id), "roundId": e.id,
                "callerId": e.caller.callerId or (str(typed.id) if typed else None),
                "callerName": (e.caller.callerName or "").strip(), "section": section, "roundDate": _iso(held),
                "company": (iv.companyName or "").strip(), "stage": getattr(e.stage, "value", e.stage),
                "engineerId": iv.createdBy, "result": result,
                "daysWaiting": (today - held_day).days if result == "awaiting" else None,
                "stale": result == "scheduled" and _utc(ends) < now,
            })
            owners.add(iv.createdBy)

    users = {u.id: u for u in await UserExtra.find({"_id": {"$in": list(owners)}}).to_list()} if owners else {}
    for r in rounds:
        u = users.get(r.pop("engineerId"))
        r["engineer"] = ((u.name or u.email) if u else "") or ""

    return {
        "period": period, "region": region, "start": start.isoformat(), "end": end.isoformat(),
        "label": label(period, start, end),
        "callers": _caller_rows(callers, rounds),
        "rounds": rounds,
    }


def _caller_rows(callers: list, rounds: list[dict]) -> list[dict]:
    """Every caller on the list (even with no rounds), then callers no longer on it that have rounds, then
    Unassigned when it has any. Rounds of a caller no longer on the list keep its id and typed name."""
    known = {str(c.id) for c in callers}
    rows = [{"callerId": str(c.id), "name": c.name, "color": c.color} for c in callers]
    for r in rounds:
        cid = r["callerId"]
        if cid and cid not in known:
            known.add(cid)
            rows.append({"callerId": cid, "name": r["callerName"] or "Removed caller", "color": None})
    if any(r["callerId"] is None for r in rounds):
        rows.append({"callerId": None, "name": "Unassigned", "color": None})
    for row in rows:
        mine = [r for r in rounds if r["callerId"] == row["callerId"]]
        counts = {k: sum(1 for r in mine if r["section"] == "period" and r["result"] == k) for k in RESULTS}
        counts["outdated"] = sum(1 for r in mine if r["section"] == "outdated")
        counts["scheduled"] = sum(1 for r in mine if r["section"] == "scheduled")
        row["counts"] = counts
        row["passRate"] = rate(counts["passed"] + counts["rejected"], counts["passed"])
    for r in rounds:
        r.pop("callerName")
    return rows
