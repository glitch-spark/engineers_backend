"""Admin team report: every non-admin's Goal vs Done for one week."""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.deps import require_admin
from app.models import DailyPlan, UserExtra
from app.models.plan_common import Counts
from app.plan_auto import week_snapshot
from app.routers._plan_access import parse_monday

router = APIRouter(prefix="/reports", tags=["reports"])


async def _last_logged(user_ids: list, until: datetime) -> dict:
    """{userId: date} of each user's latest logged day up to `until`, across weeks.

    Documents from before Goal vs Done have no `loggedAt` until migrated and
    count as logged.
    """
    rows = await DailyPlan.get_motor_collection().aggregate([
        {"$match": {
            "userId": {"$in": user_ids},
            "date": {"$lte": until},
            "$or": [{"loggedAt": {"$ne": None}}, {"loggedAt": {"$exists": False}}],
        }},
        {"$group": {"_id": "$userId", "last": {"$max": "$date"}}},
    ]).to_list(length=None)
    return {row["_id"]: row["last"].date() for row in rows}


@router.get("/team")
async def team_report(
    _admin: Annotated[UserExtra, Depends(require_admin)],
    weekStart: str = Query(...),
) -> dict:
    monday = parse_monday(weekStart)
    users = await UserExtra.find({"role": {"$ne": "admin"}}).to_list()
    users.sort(key=lambda u: ((u.name or u.email or "").lower(), str(u.id)))
    ids = [u.id for u in users]
    snaps = await week_snapshot(ids, monday)
    sunday = monday + timedelta(days=6)
    last_logged = await _last_logged(ids, datetime(sunday.year, sunday.month, sunday.day))
    rows = []
    for u in users:
        snap = snaps[u.id]
        plan = snap["plan"]
        rows.append({
            "userId": str(u.id),
            "name": u.name,
            "email": u.email,
            "goal": (plan.goal if plan else Counts()).model_dump(),
            "done": snap["done"].model_dump(),
            "itemsDone": sum(1 for i in plan.goalItems if i.done) if plan else 0,
            "itemsTotal": len(plan.goalItems) if plan else 0,
            "lastLoggedDate": last_logged[u.id].isoformat() if u.id in last_logged else None,
            "hasWeeklyPlan": plan is not None,
        })
    return {"weekStart": monday.isoformat(), "users": rows}
