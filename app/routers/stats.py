"""Statistics tabs (insights): /stats/{tab}."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Query

from app.dashboard_stats import app_tz
from app.deps import get_current_user
from app.models import UserExtra
from app.routers.dashboard import _subject_id
from app.routers.metrics import _stats_days
from app.stats.bids import bids_view
from app.stats.interviews import interviews_view
from app.stats.profiles import profiles_view

router = APIRouter(prefix="/stats", tags=["stats"])

VIEWS = {"bids": bids_view, "interviews": interviews_view, "profiles": profiles_view}


@router.get("/{tab}")
async def stats_tab(
    tab: Literal["bids", "interviews", "profiles"],
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
