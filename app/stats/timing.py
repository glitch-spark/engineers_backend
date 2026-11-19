"""When to bid (weekday / 3-hour block in the app time zone) and per-bidder quality (pure, no DB)."""
from datetime import datetime, timezone
from statistics import median
from typing import Optional

from app.stats.job_fit import _groups, _stats
from app.stats.profile_report import MIN_MATURED

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_HOURS = [f"{h:02d}–{h + 3:02d}" for h in range(0, 24, 3)]


def _local(r: dict, tz) -> datetime:
    return r["appliedAt"].replace(tzinfo=timezone.utc).astimezone(tz)  # appliedAt is naive UTC


def by_time(you: list[dict], team: list[dict], matured_before: datetime, tz) -> dict:
    you = [r for r in you if r.get("bidStatus") != "rejected"]
    team = [r for r in team if r.get("bidStatus") != "rejected"]
    return {
        "weekday": _groups(you, team, matured_before, lambda r: _local(r, tz).weekday(), list(enumerate(_DAYS))),
        "hours": _groups(you, team, matured_before, lambda r: _local(r, tz).hour // 3, list(enumerate(_HOURS))),
    }


def team_bidder_per100(rows: list[dict], matured_before: datetime) -> Optional[float]:
    by: dict = {}
    for r in rows:
        if r.get("bidderId") is not None and r.get("bidStatus") != "rejected":
            by.setdefault(r["bidderId"], []).append(r)
    rates = [s["per100"] for s in (_stats(v, matured_before) for v in by.values())
             if s["matured"] >= MIN_MATURED and s["per100"] is not None]
    return round(median(rates), 1) if rates else None


def bidders(rows: list[dict], bidder_names: dict, matured_before: datetime) -> list[dict]:
    """`rows` = the subject's rows incl. rejected; `bidder_names` = {bidderId: name} of the subject's bidders."""
    out = []
    for bid, name in bidder_names.items():
        mine = [r for r in rows if r.get("bidderId") == bid]
        if not mine:
            continue
        ok = [r for r in mine if r.get("bidStatus") != "rejected"]
        out.append({"bidderId": str(bid), "name": name, **_stats(ok, matured_before),
                    "rejectedShare": round((len(mine) - len(ok)) / len(mine) * 100)})
    return sorted(out, key=lambda b: -b["bids"])
