"""When to bid (weekday / 3-hour block in the app time zone) and per-bidder quality (pure, no DB)."""
from datetime import datetime, timezone

from app.stats.job_fit import _groups, _stats

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


def team_bidder(rows: list[dict], matured_before: datetime) -> dict:
    """Pooled over every team bidder row (non-rejected): {per100, matured, interviews}. Pooled, not a median of
    per-bidder rates: at real rates most bidders have 0 interviews, so that median is 0 almost always."""
    s = _stats([r for r in rows if r.get("bidderId") is not None and r.get("bidStatus") != "rejected"], matured_before)
    return {k: s[k] for k in ("per100", "matured", "interviews")}


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
