from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.models import UserExtra, WeeklyPlan
from app.routers._util import doc_to_dict, populate_owner, to_object_id

router = APIRouter(prefix="/weekly-plans", tags=["weekly-plans"])


class WeeklyPlanCreate(BaseModel):
    weekNumber: int
    year: int
    startDate: str
    endDate: str
    content: str
    result: Optional[str] = None


class WeeklyPlanUpdate(BaseModel):
    weekNumber: Optional[int] = None
    year: Optional[int] = None
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    content: Optional[str] = None
    result: Optional[str] = None


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@router.get("")
async def list_plans(
    user: Annotated[UserExtra, Depends(get_current_user)],
    page: int = Query(1),
    limit: int = Query(10),
    year: Optional[int] = Query(None),
    weekNumber: Optional[int] = Query(None),
    userId: Optional[str] = Query(None),
) -> dict:
    is_admin = user.role == "admin"
    query: dict[str, Any] = {}

    if not is_admin:
        query["userId"] = user.id
    elif userId:
        query["userId"] = to_object_id(userId)

    if year is not None:
        query["year"] = year
    if weekNumber is not None:
        query["weekNumber"] = weekNumber

    skip = (page - 1) * limit
    total = await WeeklyPlan.find(query).count()
    plans = (
        await WeeklyPlan.find(query)
        .sort([("year", -1), ("weekNumber", -1), ("createdAt", -1)])
        .skip(skip)
        .limit(limit)
        .to_list()
    )
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
    body: WeeklyPlanCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if not body.weekNumber or not body.year or not body.startDate or not body.endDate or not body.content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing required fields")

    existing = await WeeklyPlan.find_one({
        "userId": user.id,
        "year": body.year,
        "weekNumber": body.weekNumber,
    })
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Weekly plan already exists for this week",
        )

    plan = WeeklyPlan(
        userId=user.id,
        weekNumber=body.weekNumber,
        year=body.year,
        startDate=_parse_dt(body.startDate),
        endDate=_parse_dt(body.endDate),
        content=body.content,
        result=body.result or "",
    )
    await plan.insert()
    populated = await populate_owner([plan], field="userId", only=("name", "email"))
    return populated[0]


@router.get("/{plan_id}")
async def get_plan(
    plan_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(plan_id)
    plan = await WeeklyPlan.get(oid)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Weekly plan not found")

    is_admin = user.role == "admin"
    if not is_admin and str(plan.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    populated = await populate_owner([plan], field="userId", only=("name", "email"))
    return populated[0]


@router.put("/{plan_id}")
async def update_plan(
    plan_id: str,
    body: WeeklyPlanUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(plan_id)
    plan = await WeeklyPlan.get(oid)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Weekly plan not found")

    is_admin = user.role == "admin"
    if not is_admin and str(plan.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    if body.weekNumber is not None:
        plan.weekNumber = body.weekNumber
    if body.year is not None:
        plan.year = body.year
    if body.startDate is not None:
        plan.startDate = _parse_dt(body.startDate)
    if body.endDate is not None:
        plan.endDate = _parse_dt(body.endDate)
    if body.content is not None:
        plan.content = body.content
    if body.result is not None:
        plan.result = body.result

    await plan.save()
    populated = await populate_owner([plan], field="userId", only=("name", "email"))
    return populated[0]


@router.delete("/{plan_id}")
async def delete_plan(
    plan_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(plan_id)
    plan = await WeeklyPlan.get(oid)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Weekly plan not found")

    is_admin = user.role == "admin"
    if not is_admin and str(plan.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    await plan.delete()
    return {"message": "Weekly plan deleted successfully"}
