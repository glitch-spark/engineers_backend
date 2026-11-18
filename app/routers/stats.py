"""Statistics tabs (insights): /stats/{tab}."""

from __future__ import annotations

from datetime import date as date_cls, datetime
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Query

from app.applications.rows import interview_outcome
from app.applications.sync import BACKFILL_SINCE
from app.applications.window import response_window
from app.dashboard_stats import app_tz
from app.deps import get_current_user, require_admin
from app.models import Application, Interview, UserExtra
from app.routers.dashboard import _subject_id
from app.routers.metrics import _stats_days
from app.stats.ai_usage import ai_usage_view
from app.stats.bids import bids_view
from app.stats.interviews import interviews_view

router = APIRouter(prefix="/stats", tags=["stats"])

VIEWS = {"bids": bids_view, "interviews": interviews_view, "ai-usage": ai_usage_view}


@router.get("/{tab}")
async def stats_tab(
    tab: Literal["bids", "interviews", "ai-usage"],
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: Literal["week", "month", "custom"] = Query("month"),
    date: str = Query("", description="YYYY-MM-DD: any day in the week or month; custom: the first day"),
    to: str = Query("", description="YYYY-MM-DD: a custom range's last day"),
    region: Literal["all", "Latam", "EU", "US"] = Query("all"),
    user_id: Optional[str] = Query(None, alias="user", description="admin only: view this user"),
) -> dict:
    day, until = _stats_days(period, date, to)
    # _subject_id 403s any non-admin id, including their own; own id means "self".
    sid = user.id if user_id and user_id == str(user.id) else await _subject_id(user, user_id)
    subject = user if sid == user.id else await UserExtra.get(sid)
    return await VIEWS[tab](subject, period, day, region, until, datetime.now(app_tz()).date())


@router.get("/applications/coverage")
async def applications_coverage(
    _: Annotated[UserExtra, Depends(require_admin)],
    since: date_cls = Query(BACKFILL_SINCE, description="YYYY-MM-DD: rows applied on or after this day"),
) -> dict:
    """Admin debug view of the `applications` table: how much of the data is tied to jobs and interviews."""
    since_dt = datetime(since.year, since.month, since.day)
    apps = Application.get_motor_collection()
    rows = [d async for d in apps.find({"appliedAt": {"$gte": since_dt}}, {"key": 1, "board": 1, "interviewId": 1})]
    boards: dict[str, int] = {}
    for r in rows:
        if r.get("board"):
            boards[r["board"]] = boards.get(r["board"], 0) + 1
    # Interviews created in the period (projected, one query) that count and that no row of the table carries.
    carried = set(await apps.distinct("interviewId"))
    ivs = await Interview.get_motor_collection().find(
        {"createdAt": {"$gte": since_dt}},
        {f: 1 for f in ("accountId", "createdBy", "scheduledAt", "stage", "status", "stageHistory", "createdAt", "updatedAt")},
    ).to_list(None)
    unmatched = sum(1 for d in ivs if d["_id"] not in carried and interview_outcome(Interview.model_validate(d)))
    state = await apps.database["app_settings"].find_one({"_id": "applications_sync"}) or {}
    return {
        "rows": len(rows),
        "matched": sum(1 for r in rows if r.get("interviewId")),
        "unmatchedInterviews": unmatched,
        "noLink": sum(1 for r in rows if r["key"].startswith("src:")),
        "byBoard": [{"board": b, "rows": n} for b, n in sorted(boards.items(), key=lambda x: (-x[1], x[0]))[:15]],
        "window": await response_window(datetime.now(app_tz()).date()),
        "lastSync": {"checkpoint": state.get("checkpoint"), "lastFull": state.get("lastFull")},
    }
