from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.models import UserExtra, WeeklyPlan
from app.models.weekly_plan import CORE_METRICS, WeeklyMetric
from app.routers._util import doc_to_dict, populate_owner, to_object_id

router = APIRouter(prefix="/weekly-plans", tags=["weekly-plans"])


def _naive_dt(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _plan_week_bounds(plan: WeeklyPlan) -> tuple[datetime, datetime]:
    """Monday 00:00 → Sunday 23:59:59.999 from the plan's startDate."""
    start = _naive_dt(plan.startDate).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=6, hours=23, minutes=59, seconds=59, microseconds=999000)
    return start, end


def _next_week_bounds(week_start: datetime) -> tuple[datetime, datetime]:
    next_start = week_start + timedelta(days=7)
    next_end = next_start + timedelta(days=6, hours=23, minutes=59, seconds=59, microseconds=999000)
    return next_start, next_end


def _set_interviews_metric(metrics: list[WeeklyMetric], *, target: int, actual: int) -> list[WeeklyMetric]:
    out: list[WeeklyMetric] = []
    found = False
    for m in metrics:
        if m.key == "interviews":
            out.append(WeeklyMetric(
                key="interviews",
                label=m.label or "Interviews",
                target=max(0, int(target)),
                actual=max(0, int(actual)),
                unit="count",
            ))
            found = True
        else:
            out.append(m)
    if not found:
        out.append(WeeklyMetric(
            key="interviews",
            label="Interviews",
            target=max(0, int(target)),
            actual=max(0, int(actual)),
            unit="count",
        ))
    return out


async def _apply_badge_interview_counts(plan: WeeklyPlan) -> None:
    """Overwrite interviews metrics from stage-badge board counts (not free text).

    - interviews.actual = countable badges in this plan's week
    - interviews.target = prior week's nextInterviewTarget when set (else keep
      any existing target, typically 0 after we clear AI interview numbers)
    - nextInterviewTarget = countable badges already scheduled next week
    """
    from app.routers.metrics import _interview_count

    start, end = _plan_week_bounds(plan)
    next_start, next_end = _next_week_bounds(start)

    this_week = await _interview_count(plan.userId, start, end)
    next_week = await _interview_count(plan.userId, next_start, next_end)

    # Prefer the forward-looking goal set on the previous week's review.
    prev_start = start - timedelta(days=7)
    prev_plan = await WeeklyPlan.find_one({
        "userId": plan.userId,
        "startDate": {
            "$gte": prev_start - timedelta(hours=12),
            "$lte": prev_start + timedelta(hours=12),
        },
    })
    if prev_plan is None:
        # Fallback: previous calendar weekNumber/year (handles year wrap).
        prev_week = plan.weekNumber - 1
        prev_year = plan.year
        if prev_week < 1:
            prev_week = 52
            prev_year = plan.year - 1
        prev_plan = await WeeklyPlan.find_one({
            "userId": plan.userId,
            "year": prev_year,
            "weekNumber": prev_week,
        })

    existing_target = 0
    for m in (plan.metrics or []):
        if m.key == "interviews":
            existing_target = int(m.target or 0)
            break
    target = existing_target
    if prev_plan is not None and prev_plan.nextInterviewTarget is not None:
        target = max(0, int(prev_plan.nextInterviewTarget))

    plan.metrics = _set_interviews_metric(list(plan.metrics or []), target=target, actual=this_week)
    plan.nextInterviewTarget = next_week


class MetricIn(BaseModel):
    key: str
    label: str
    target: int = 0
    actual: int = 0
    unit: Optional[str] = None


def _clean_metrics(items: Optional[list[MetricIn]]) -> list[WeeklyMetric]:
    out: list[WeeklyMetric] = []
    for m in items or []:
        key = (m.key or "").strip()
        label = (m.label or "").strip()
        if not key or not label:
            continue
        out.append(WeeklyMetric(
            key=key,
            label=label,
            target=max(0, int(m.target or 0)),
            actual=max(0, int(m.actual or 0)),
            unit="hours" if (m.unit or "").strip().lower() == "hours" else "count",
        ))
    return out


class WeeklyPlanCreate(BaseModel):
    weekNumber: int
    year: int
    startDate: str
    endDate: str
    content: Optional[str] = None
    result: Optional[str] = None
    metrics: Optional[list[MetricIn]] = None
    status: Optional[str] = None
    nextInterviewTarget: Optional[int] = None


class WeeklyPlanUpdate(BaseModel):
    weekNumber: Optional[int] = None
    year: Optional[int] = None
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    content: Optional[str] = None
    result: Optional[str] = None
    metrics: Optional[list[MetricIn]] = None
    status: Optional[str] = None
    nextInterviewTarget: Optional[int] = None


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@router.post("/progress-report")
async def progress_report(
    user: Annotated[UserExtra, Depends(get_current_user)],
    year: Optional[int] = Query(None),
    weekNumber: Optional[int] = Query(None),
    userId: Optional[str] = Query(None),
) -> dict:
    """Run AI extraction over the weekly plans in scope (free-text plan/
    follow-up -> structured metrics), persist the metrics onto each plan,
    and return a per-user rollup. Re-running overwrites (picks up text
    edits). Non-admins may only re-analyze their own plans (to avoid
    rewriting teammates' metrics); admins may scope via `userId`.

    Interview counts are NOT taken from free text — after extraction they are
    overwritten from stage-badge board counts for this week and next week
    (`nextInterviewTarget`)."""
    from app.weekly_report import extract_metrics

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

    plans = await WeeklyPlan.find(query).sort([("year", 1), ("weekNumber", 1)]).to_list()
    # Guardrail: cap how many plans one run will process (cost control).
    # Process oldest → newest so each week's nextInterviewTarget can seed
    # the following week's interviews target in the same run.
    MAX_PLANS = 200
    capped = plans[:MAX_PLANS]

    processed = 0
    for p in capped:
        metrics = await extract_metrics(p.content or "", p.result or "")
        # Drop any AI-extracted interview numbers — board badges are source of truth.
        p.metrics = _set_interviews_metric(metrics, target=0, actual=0)
        await _apply_badge_interview_counts(p)
        if (p.result or "").strip():
            p.status = "reviewed"
        await p.save()
        processed += 1

    populated = await populate_owner(capped, field="userId", only=("name", "email"))
    return {
        "processed": processed,
        "skipped": max(0, len(plans) - len(capped)),
        "plans": populated,
    }


@router.get("/user-rollup")
async def user_rollup(
    _: Annotated[UserExtra, Depends(get_current_user)],
    year: Optional[int] = Query(None),
    weekNumber: Optional[int] = Query(None),
    userId: Optional[str] = Query(None),
) -> dict:
    """Aggregate stored metrics per user across the scoped weeks
    (planned vs actual bids/interviews/etc). Pure aggregation — no AI call;
    reads metrics populated by Run Progress Report. Any authenticated user
    may view the team rollup; optional `userId` filters to one person."""
    query: dict[str, Any] = {}
    if year is not None:
        query["year"] = year
    if weekNumber is not None:
        query["weekNumber"] = weekNumber
    if userId:
        query["userId"] = to_object_id(userId)

    plans = await WeeklyPlan.find(query).to_list()

    # Group by user.
    by_user: dict[str, dict] = {}
    for p in plans:
        uid = str(p.userId)
        u = by_user.setdefault(uid, {
            "userId": uid, "planCount": 0, "reviewedCount": 0, "metrics": {},
        })
        u["planCount"] += 1
        if p.status == "reviewed":
            u["reviewedCount"] += 1
        for m in (p.metrics or []):
            agg = u["metrics"].setdefault(m.key, {"label": m.label, "target": 0, "actual": 0, "unit": "count"})
            agg["target"] += m.target
            agg["actual"] += m.actual
            if m.label:
                agg["label"] = m.label
            agg["unit"] = getattr(m, "unit", "count") or "count"

    # Populate owner name/email.
    owners = await UserExtra.find({"_id": {"$in": [to_object_id(uid) for uid in by_user]}}).to_list() if by_user else []
    name_by_id = {str(o.id): (o.name or "", o.email or "") for o in owners}

    rows = []
    for uid, u in by_user.items():
        nm, em = name_by_id.get(uid, ("", ""))
        rows.append({
            **u,
            "name": nm,
            "email": em,
            "metrics": [{"key": k, **v} for k, v in u["metrics"].items()],
        })
    rows.sort(key=lambda r: (r["name"] or r["email"] or "").lower())
    return {"users": rows}


@router.get("/metric-catalog")
async def metric_catalog(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Recommended core metrics — frontend seeds new plans from this."""
    return {"metrics": CORE_METRICS}


@router.get("/summary")
async def plans_summary(
    _: Annotated[UserExtra, Depends(get_current_user)],
    year: Optional[int] = Query(None),
    weekNumber: Optional[int] = Query(None),
    userId: Optional[str] = Query(None),
) -> dict:
    """Aggregate metrics across weeks for the stats header. Scope follows
    the same filters as the list (year + optional weekNumber + optional
    userId). Any authenticated user can view team-wide stats."""
    query: dict[str, Any] = {}
    if userId:
        query["userId"] = to_object_id(userId)
    if year is not None:
        query["year"] = year
    if weekNumber is not None:
        query["weekNumber"] = weekNumber

    plans = (
        await WeeklyPlan.find(query)
        .sort([("year", 1), ("weekNumber", 1)])
        .to_list()
    )

    series: list[dict] = []
    totals: dict[str, dict] = {}   # key -> {label, target, actual}
    reviewed = 0
    for p in plans:
        if p.status == "reviewed":
            reviewed += 1
        row: dict[str, Any] = {
            "weekNumber": p.weekNumber,
            "year": p.year,
            "label": f"W{p.weekNumber}",
            "status": p.status,
        }
        for m in (p.metrics or []):
            row[f"{m.key}_target"] = m.target
            row[f"{m.key}_actual"] = m.actual
            agg = totals.setdefault(m.key, {"label": m.label, "target": 0, "actual": 0})
            agg["target"] += m.target
            agg["actual"] += m.actual
            # keep latest non-empty label
            if m.label:
                agg["label"] = m.label
        series.append(row)

    total_plans = len(plans)
    return {
        "series": series,
        "totals": [{"key": k, **v} for k, v in totals.items()],
        "planCount": total_plans,
        "reviewedCount": reviewed,
        "completionRate": round(reviewed / total_plans, 3) if total_plans else 0.0,
    }


@router.get("")
async def list_plans(
    _: Annotated[UserExtra, Depends(get_current_user)],
    page: int = Query(1),
    limit: int = Query(10),
    year: Optional[int] = Query(None),
    weekNumber: Optional[int] = Query(None),
    userId: Optional[str] = Query(None),
) -> dict:
    """List weekly plans for the team. Any authenticated user can read all
    plans (so people can see how others are working). Optional `userId`
    filters to one owner. Create/update/delete remain owner-or-admin."""
    query: dict[str, Any] = {}

    if userId:
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
    if not body.weekNumber or not body.year or not body.startDate or not body.endDate:
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
        content=body.content or "",
        result=body.result or "",
        metrics=_clean_metrics(body.metrics),
        status=body.status if body.status in ("planned", "reviewed") else "planned",
        nextInterviewTarget=body.nextInterviewTarget,
    )
    await plan.insert()
    populated = await populate_owner([plan], field="userId", only=("name", "email"))
    return populated[0]


@router.get("/{plan_id}")
async def get_plan(
    plan_id: str,
    _: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(plan_id)
    plan = await WeeklyPlan.get(oid)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Weekly plan not found")

    # Reads are open to any authenticated user (team visibility).
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
    if body.metrics is not None:
        plan.metrics = _clean_metrics(body.metrics)
    if body.status in ("planned", "reviewed"):
        plan.status = body.status
    if body.nextInterviewTarget is not None:
        plan.nextInterviewTarget = max(0, int(body.nextInterviewTarget))

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
