from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.alerts import app_week_key
from app.config import get_settings
from app.daily_plan_stats import METRICS, PERIODS, build_series, sum_totals, window_start
from app.deps import get_current_user
from app.models import UserExtra
from app.models.daily_plan import DailyPlan
from app.routers._util import populate_owner, to_object_id
from app.slack_bot import delete_daily_plan_message, sync_daily_plan_message

router = APIRouter(prefix="/daily-plans", tags=["daily-plans"])

PLAN_TYPES = ("custom", "regular")
MAX_ITEMS = 20
MAX_ITEM_LENGTH = 200
ITEM_FIELDS = ("todayItems", "tomorrowItems")


class DailyPlanUpdate(BaseModel):
    date: Optional[str] = None
    planType: Optional[str] = None
    today: Optional[str] = None
    tomorrow: Optional[str] = None
    bidsHandsOn: Optional[int] = None
    bidsByBidder: Optional[int] = None
    interviewsDone: Optional[int] = None
    interviewsNew: Optional[int] = None
    todayItems: Optional[list[str]] = None
    planBidsHandsOn: Optional[int] = None
    planBidsByBidder: Optional[int] = None
    planInterviewsScheduled: Optional[int] = None
    tomorrowItems: Optional[list[str]] = None


class DailyPlanCreate(DailyPlanUpdate):
    date: str


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


async def _sync_slack(plan: DailyPlan, owner: UserExtra) -> None:
    """Post or edit the plan's Slack message; remember where it lives."""
    posted = await sync_daily_plan_message(plan, owner)
    if posted and posted != (plan.slackChannel, plan.slackTs):
        plan.slackChannel, plan.slackTs = posted
        await plan.save()


def _apply_fields(plan: DailyPlan, body: DailyPlanUpdate) -> None:
    """Copy the plan type, notes and counts that were sent; 400 on bad values."""
    if body.planType is not None:
        if body.planType not in PLAN_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="planType must be 'custom' or 'regular'",
            )
        plan.planType = body.planType
    if body.today is not None:
        plan.today = body.today
    if body.tomorrow is not None:
        plan.tomorrow = body.tomorrow
    for field in METRICS:
        value = getattr(body, field)
        if value is None:
            continue
        if value < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field} cannot be negative",
            )
        setattr(plan, field, value)
    for field in ITEM_FIELDS:
        raw = getattr(body, field)
        if raw is None:
            continue
        items = [item.strip() for item in raw if item.strip()]
        if len(items) > MAX_ITEMS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"At most {MAX_ITEMS} custom items per day",
            )
        if any(len(item) > MAX_ITEM_LENGTH for item in items):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Custom items can be at most {MAX_ITEM_LENGTH} characters",
            )
        setattr(plan, field, items)


def _slack_snapshot(plan: DailyPlan) -> tuple:
    """Everything the Slack post shows, so unchanged saves skip the edit."""
    return (
        plan.date,
        plan.planType,
        plan.today,
        plan.tomorrow,
        *(getattr(plan, m) for m in METRICS),
        *(tuple(getattr(plan, f)) for f in ITEM_FIELDS),
    )


def _app_today() -> date:
    tz_name = (get_settings().APP_TIMEZONE or "").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


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


@router.get("/stats")
async def plan_stats(
    user: Annotated[UserExtra, Depends(get_current_user)],
    bucket: Literal["day", "week", "month"] = Query("day"),
    userId: Optional[str] = Query(None),
    to: Optional[str] = Query(None, description="Last day of the window (YYYY-MM-DD)"),
) -> dict:
    """Regular-plan counts per day, week or month for the dashboard chart.

    The window ends on `to` (the caller's local today; APP_TIMEZONE's today
    when omitted) and covers the last 14 days, 12 weeks or 12 months. Without
    `userId` it sums the whole team. Readable by any signed-in user, like the
    plan list.
    """
    today = _parse_day(to).date() if to else _app_today()
    periods = PERIODS[bucket]
    start = window_start(today, bucket, periods)
    # The plan dated the day before the window holds the window's first planned day.
    first = start - timedelta(days=1)

    query: dict[str, Any] = {
        "planType": "regular",
        "date": {
            "$gte": datetime(first.year, first.month, first.day),
            "$lte": datetime(today.year, today.month, today.day),
        },
    }
    if userId:
        query["userId"] = to_object_id(userId)
    plans = await DailyPlan.find(query).to_list()

    series = build_series(plans, today, bucket, periods)
    return {
        "bucket": bucket,
        "window": {"from": start.isoformat(), "to": today.isoformat()},
        "series": series,
        "totals": sum_totals(series),
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

    plan = DailyPlan(userId=user.id, date=day, year=year, weekNumber=week_number)
    _apply_fields(plan, body)
    try:
        await plan.insert()
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Daily plan already exists for this date",
        )
    await _sync_slack(plan, user)
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

    before = _slack_snapshot(plan)
    if body.date is not None:
        if not body.date.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Date is required")
        day = _parse_day(body.date)
        await _duplicate_day(plan.userId, day, ignore_id=plan.id)
        year, week_number = _week_fields(day)
        plan.date = day
        plan.year = year
        plan.weekNumber = week_number
    _apply_fields(plan, body)

    try:
        await plan.save()
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Daily plan already exists for this date",
        )
    if _slack_snapshot(plan) != before or not plan.slackTs:
        await _sync_slack(plan, user)
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
    await delete_daily_plan_message(plan)
    return {"message": "Daily plan deleted successfully"}
