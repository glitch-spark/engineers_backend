"""Admin team report: every non-admin's Goal vs Done for one week."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.deps import require_admin
from app.models import UserExtra
from app.models.plan_common import Counts
from app.plan_auto import week_snapshot
from app.routers._plan_access import parse_monday

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/team")
async def team_report(
    _admin: Annotated[UserExtra, Depends(require_admin)],
    weekStart: str = Query(...),
) -> dict:
    monday = parse_monday(weekStart)
    users = await UserExtra.find({"role": {"$ne": "admin"}}).to_list()
    users.sort(key=lambda u: ((u.name or u.email or "").lower(), str(u.id)))
    snaps = await week_snapshot([u.id for u in users], monday)
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
            "lastLoggedDate": snap["lastLogged"].isoformat() if snap["lastLogged"] else None,
            "hasWeeklyPlan": plan is not None,
        })
    return {"weekStart": monday.isoformat(), "users": rows}
