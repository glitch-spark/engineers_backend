"""When to bid: weekday / 3-hour block in the app time zone (pure, no DB)."""
from datetime import datetime, timezone

from app.stats.job_fit import _groups

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
