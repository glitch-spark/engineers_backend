"""The Conversion tab's scope: whose rows (me / one profile / the team) on non-archived profiles of their CURRENT
region (Account.region, not the row's region at sync time), for a period clamped to DATA_START."""
from __future__ import annotations

from datetime import date as Date, datetime, timedelta
from typing import Literal, Optional

from fastapi import HTTPException, status

from app import interview_rates
from app.dashboard_stats import app_tz, local_day_start_utc
from app.models import Account, UserExtra
from app.routers._util import to_object_id
from app.stats.profile_loader import load_period_rows

DATA_START = Date(2026, 10, 6)  # bids before it are incomplete: no period of this tab starts earlier


def bounds(start_d: Date, end_d: Date) -> tuple[datetime, datetime]:
    """Local days [start_d, end_d] -> naive-UTC [start, end)."""
    tz = app_tz()
    return local_day_start_utc(start_d, tz), local_day_start_utc(end_d + timedelta(days=1), tz)


async def conversion_scope(user: UserExtra, period: str, date: str, to: str, region: str, user_id: Optional[str],
                           scope: Literal["me", "profile", "team"] = "me", profile: Optional[str] = None) -> dict:
    """Query params -> subject, clamped bounds and the loader rows of the scope (`rows`) and of the team (`teamRows`,
    every non-archived profile of the region: the baseline). Rows still include rejected bids; callers drop them."""
    from app.routers.dashboard import _subject_id
    from app.routers.metrics import _stats_days

    day, until = _stats_days(period, date, to)
    # _subject_id 403s any non-admin id, including their own; own id means "self".
    sid = user.id if user_id and user_id == str(user.id) else await _subject_id(user, user_id)
    subject = user if sid == user.id else await UserExtra.get(sid)
    start_d, end_d = interview_rates.window(period, day, until)
    clamped = start_d < DATA_START
    label = interview_rates.label(period, start_d, end_d)
    if clamped and end_d >= DATA_START:
        start_d = DATA_START
        label = interview_rates.label(period, start_d, end_d)
    q: dict = {"archived": {"$ne": True}}
    if region != "all":
        q["region"] = region
    team = {str(d["_id"]): {"name": d.get("name"), "region": d.get("region"), "archived": bool(d.get("archived")),
                            "ownerId": d.get("createdBy")}
            async for d in Account.get_motor_collection().find(q, {"name": 1, "region": 1, "archived": 1, "createdBy": 1})}
    if scope == "profile":
        if not profile:
            raise HTTPException(status_code=422, detail="scope=profile needs profile")
        acct = await Account.get_motor_collection().find_one({"_id": to_object_id(profile)}, {"createdBy": 1})
        if acct is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="profile not found")
        if acct.get("createdBy") != sid and user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not your profile")
        ids = {str(acct["_id"])} & team.keys()  # archived / other region: an empty scope, not an error
    elif scope == "me":
        ids = {k for k, a in team.items() if a["ownerId"] == sid}
    else:
        ids = set(team)
    start, end = bounds(start_d, end_d)
    # region "all" + the account filter: one cached load per period whatever the region (rows keep their sync region)
    loaded = await load_period_rows(start, end, "all") if end_d >= DATA_START else []
    team_rows = [r for r in loaded if str(r.get("accountId")) in team]
    tz = app_tz()
    return {"sid": sid, "subject": {"userId": str(sid), "name": subject.name}, "scope": scope,
            "start_d": start_d, "end_d": end_d, "start": start, "end": end, "label": label, "startClamped": clamped,
            "now": datetime.utcnow(), "today": datetime.now(tz).date(),
            "accounts": {k: team[k] for k in ids}, "rows": [r for r in team_rows if str(r["accountId"]) in ids],
            "teamRows": team_rows}
