"""Statistics tabs (insights): /stats/{tab}."""

from __future__ import annotations

from datetime import date as date_cls, datetime, timedelta
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Query

from app.applications.sync import BACKFILL_SINCE
from app.applications.window import response_window
from app import interview_rates
from app.dashboard_stats import app_tz, local_day_start_utc
from app.deps import get_current_user, require_admin
from app.models import Application, UserExtra
from app.routers.dashboard import _subject_id
from app.routers.metrics import _stats_days
from app.stats.ai_usage import ai_usage_view
from app.stats.bids import bids_view
from app.stats.interviews import interviews_view
from app.stats.job_fit import breakdown
from app.stats.profile_loader import count_unmatched_interviews, load_period_rows
from app.stats.profile_report import grade, summarize, team_baseline
from app.plan_profiles import owner_profiles

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
    unmatched = await count_unmatched_interviews(since_dt)
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


_GRADE_ORDER = {"strong": 0, "watch": 1, "dead": 2, "early": 3}


async def _scope(user: UserExtra, period: str, date: str, to: str, user_id: Optional[str]) -> dict:
    """Shared by the applications cards: query params -> subject, naive-UTC period bounds, label, now, response window."""
    day, until = _stats_days(period, date, to)
    sid = user.id if user_id and user_id == str(user.id) else await _subject_id(user, user_id)
    subject = user if sid == user.id else await UserExtra.get(sid)
    start_d, end_d = interview_rates.window(period, day, until)
    tz = app_tz()
    return {"sid": sid, "subject": {"userId": str(sid), "name": subject.name}, "start_d": start_d, "end_d": end_d,
            "start": local_day_start_utc(start_d, tz), "end": local_day_start_utc(end_d + timedelta(days=1), tz),
            "label": interview_rates.label(period, start_d, end_d), "now": datetime.utcnow(),
            "today": datetime.now(tz).date(), "win": await response_window(datetime.now(tz).date())}


@router.get("/applications/profiles")
async def applications_profiles(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: Literal["week", "month", "custom"] = Query("month"),
    date: str = Query("", description="YYYY-MM-DD: any day in the week or month; custom: the first day"),
    to: str = Query("", description="YYYY-MM-DD: a custom range's last day"),
    region: Literal["all", "Latam", "EU", "US"] = Query("all"),
    user_id: Optional[str] = Query(None, alias="user", description="admin only: view this user"),
) -> dict:
    """The "Your profiles" report (cohort = rows applied in the period) and the coverage numbers."""
    sc = await _scope(user, period, date, to, user_id)
    sid, start_d, end_d, start, end, now, win = (sc[k] for k in ("sid", "start_d", "end_d", "start", "end", "now", "win"))
    rows = await load_period_rows(start, end, region)
    summaries = summarize(rows, now - timedelta(days=win["matured_days"]))
    team = team_baseline(summaries, rows)
    mine = [p for p in await owner_profiles(sid) if p["id"] in summaries]
    profiles = []
    for p in mine:
        s = summaries[p["id"]]
        g, reasons = grade(s, team)
        profiles.append({"accountId": p["id"], "name": p["name"], "region": p["region"], "archived": p["archived"],
                         **s, "grade": g, "reasons": reasons})
    profiles.sort(key=lambda x: (_GRADE_ORDER[x["grade"]], -(x["per100"] if x["per100"] is not None else -1), x["name"]))
    own = [r for r in rows if r["ownerId"] == sid and r.get("bidStatus") != "rejected"]
    regions, _ = await interview_rates._profile_regions()
    header = (await interview_rates.counts([sid], start_d, end_d, region, regions, {}, logged_only=False)).get(sid, (0,))[0]
    return {
        "label": sc["label"],
        "subject": sc["subject"],
        "window": win,
        "coverage": {"rows": len(own), "headerBids": header, "noLink": sum(1 for r in own if r["key"].startswith("src:")),
                     "unmatchedInterviews": await count_unmatched_interviews(start, end, sid, region, regions)},
        "team": team,
        "profiles": profiles,
    }


@router.get("/applications/jobs")
async def applications_jobs(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: Literal["week", "month", "custom"] = Query("month"),
    date: str = Query("", description="YYYY-MM-DD: any day in the week or month; custom: the first day"),
    to: str = Query("", description="YYYY-MM-DD: a custom range's last day"),
    region: Literal["all", "Latam", "EU", "US"] = Query("all"),
    user_id: Optional[str] = Query(None, alias="user", description="admin only: view this user"),
) -> dict:
    """"What kind of job works": interviews per 100 matured bids by site, score, area and profile age."""
    sc = await _scope(user, period, date, to, user_id)
    rows = await load_period_rows(sc["start"], sc["end"], region)
    mine = {p["id"] for p in await owner_profiles(sc["sid"])}
    you = [r for r in rows if str(r.get("accountId")) in mine]
    cutoff = sc["now"] - timedelta(days=sc["win"]["matured_days"])
    return {"label": sc["label"], "subject": sc["subject"], "window": sc["win"], **breakdown(you, rows, cutoff)}
