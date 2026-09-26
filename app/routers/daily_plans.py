from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.alerts import app_week_key
from app.deps import get_current_user
from app.models import UserExtra
from app.models.daily_plan import DailyPlan
from app.routers._util import populate_owner, to_object_id

router = APIRouter(prefix="/daily-plans", tags=["daily-plans"])


class DailyPlanCreate(BaseModel):
    date: str
    today: Optional[str] = None
    tomorrow: Optional[str] = None


class DailyPlanUpdate(BaseModel):
    date: Optional[str] = None
    today: Optional[str] = None
    tomorrow: Optional[str] = None


def _reject_admin(user: UserExtra) -> None:
    if user.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admins can view daily plans but cannot change them",
        )


def _parse_day(value: str) -> datetime:
    raw = (value or "").strip()
    if len(raw) >= 10:
        raw = raw[:10]
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid date")
    return datetime(parsed.year, parsed.month, parsed.day)


def _week_fields(day: datetime) -> tuple[int, int]:
    year, week_number, _key = app_week_key(day)
    return year, week_number


async def _duplicate_day(user_id, day: datetime, ignore_id=None) -> None:
    existing = await DailyPlan.find_one({"userId": user_id, "date": day})
    if existing is not None and existing.id != ignore_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Daily plan already exists for this date",
        )


@router.get("")
async def list_plans(
    user: Annotated[UserExtra, Depends(get_current_user)],
    page: int = Query(1),
    limit: int = Query(10),
    year: Optional[int] = Query(None),
    weekNumber: Optional[int] = Query(None),
    userId: Optional[str] = Query(None),
) -> dict:
    """List daily plans for the team. Any authenticated user can read all
    plans. Admins are sorted newest-created first; everyone else by plan
    date, newest day first."""
    query: dict[str, Any] = {}
    if userId:
        query["userId"] = to_object_id(userId)
    if year is not None:
        query["year"] = year
    if weekNumber is not None:
        query["weekNumber"] = weekNumber

    skip = (page - 1) * limit
    total = await DailyPlan.find(query).count()
    sort = [("createdAt", -1)] if user.role == "admin" else [("date", -1), ("createdAt", -1)]
    plans = await DailyPlan.find(query).sort(sort).skip(skip).limit(limit).to_list()
    plans_out = await populate_owner(plans, field="userId", only=("name", "email"))

    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "plans": plans_out,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "totalPages": total_pages,
            "hasNext": page < total_pages,
            "hasPrev": page > 1,
        },
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_plan(
    body: DailyPlanCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    _reject_admin(user)
    if not body.date or not body.date.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Date is required")

    day = _parse_day(body.date)
    year, week_number = _week_fields(day)
    await _duplicate_day(user.id, day)

    plan = DailyPlan(
        userId=user.id,
        date=day,
        year=year,
        weekNumber=week_number,
        today=body.today or "",
        tomorrow=body.tomorrow or "",
    )
    try:
        await plan.insert()
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Daily plan already exists for this date",
        )
    populated = await populate_owner([plan], field="userId", only=("name", "email"))
    return populated[0]


@router.put("/{plan_id}")
async def update_plan(
    plan_id: str,
    body: DailyPlanUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    _reject_admin(user)
    plan = await DailyPlan.get(to_object_id(plan_id))
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Daily plan not found")
    if str(plan.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    if body.date is not None:
        if not body.date.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Date is required")
        day = _parse_day(body.date)
        await _duplicate_day(plan.userId, day, ignore_id=plan.id)
        year, week_number = _week_fields(day)
        plan.date = day
        plan.year = year
        plan.weekNumber = week_number
    if body.today is not None:
        plan.today = body.today
    if body.tomorrow is not None:
        plan.tomorrow = body.tomorrow

    try:
        await plan.save()
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Daily plan already exists for this date",
        )
    populated = await populate_owner([plan], field="userId", only=("name", "email"))
    return populated[0]


@router.delete("/{plan_id}")
async def delete_plan(
    plan_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    _reject_admin(user)
    plan = await DailyPlan.get(to_object_id(plan_id))
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Daily plan not found")
    if str(plan.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    await plan.delete()
    return {"message": "Daily plan deleted successfully"}
