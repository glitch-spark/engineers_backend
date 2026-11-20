"""Statistics tabs (insights): /stats/{tab}."""

from __future__ import annotations

from datetime import date as date_cls, datetime, timedelta
from typing import Annotated, Literal, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, Query

from app.applications.sync import BACKFILL_SINCE
from app.applications.window import response_window
from app import interview_rates
from app.dashboard_stats import app_tz
from app.deps import get_current_user, require_admin
from app.models import Account, Application, UserExtra
from app.models.bidder import Bidder
from app.routers.dashboard import _subject_id
from app.routers.metrics import _stats_days
from app.stats import changes, conversion, conversion_scope as cs, timing
from app.stats.ai_usage import ai_usage_view
from app.stats.bids import bids_view
from app.stats.conversion_scope import bounds as _bounds
from app.stats.frame import previous_window
from app.stats.interviews import interviews_view
from app.stats.profile_loader import accounts_with_rounds_since, count_unmatched_interviews, count_untied_interviews, load_period_rows
from app.stats.profile_report import grade, summarize, team_baseline

router = APIRouter(prefix="/stats", tags=["stats"])

VIEWS = {"bids": bids_view, "interviews": interviews_view, "ai-usage": ai_usage_view}


@router.get("/conversion")
async def conversion_report(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: Literal["week", "month", "custom"] = Query("month"),
    date: str = Query("", description="YYYY-MM-DD: any day in the week or month; custom: the first day"),
    to: str = Query("", description="YYYY-MM-DD: a custom range's last day"),
    region: Literal["all", "Latam", "EU", "US"] = Query("all"),
    user_id: Optional[str] = Query(None, alias="user", description="admin only: view this user"),
    scope: Literal["me", "profile", "team"] = Query("me", description="me: the subject's profiles; team: every profile"),
    profile: Optional[str] = Query(None, description="scope=profile: the profile (Account) id"),
) -> dict:
    """The Conversion tab: totals and breakdowns of the scope's bids (by bidder only for me / profile)."""
    sc = await cs.conversion_scope(user, period, date, to, region, user_id, scope, profile)
    win = await response_window(sc["today"])
    owners = list({a["ownerId"] for a in sc["accounts"].values()})
    names = {} if scope == "team" or not owners else {d["_id"]: d.get("name") async for d in Bidder.get_motor_collection(
    ).find({"createdBy": {"$in": owners}}, {"name": 1})}
    cutoff = sc["now"] - timedelta(days=win["matured_days"])
    team_rate = team_baseline(summarize(sc["teamRows"], cutoff), sc["teamRows"])["rate"]
    rep = conversion.report(sc["rows"], cutoff, {k: v for k, v in names.items() if v}, scope != "team", team_rate)
    untied = await count_untied_interviews([PydanticObjectId(i) for i in sc["accounts"]], sc["start"], sc["end"]
                                           ) if sc["end_d"] >= cs.DATA_START else 0
    return {"label": sc["label"], "startClamped": sc["startClamped"], "window": win, "scope": scope,
            "subject": sc["subject"], **({"profile": profile} if scope == "profile" else {}),
            **rep, "totals": {**rep["totals"], "untied": untied}}


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


async def _graded_profiles(accounts: dict, summaries: dict, team: dict, start: datetime) -> list[dict]:
    """The scope's profiles (conversion_scope `accounts`) with rows in the cohort, graded, in the profiles card order.
    A profile with an interview round since `start` is never dead: its interviews may be carried by no row."""
    profiles = []
    for pid, a in accounts.items():
        if pid not in summaries:
            continue
        s = summaries[pid]
        g, reasons = grade(s, team)
        profiles.append({"accountId": pid, "name": a["name"], "region": a["region"], "archived": a["archived"],
                         **s, "grade": g, "reasons": reasons})
    dead = [p for p in profiles if p["grade"] == "dead"]
    busy = await accounts_with_rounds_since([PydanticObjectId(p["accountId"]) for p in dead], start)
    for p in dead:
        if p["accountId"] in busy:
            p["grade"], p["reasons"] = "watch", p["reasons"] + [
                "but it has interviews in the period that no tracked bid carries"]
    profiles.sort(key=lambda x: (_GRADE_ORDER[x["grade"]], -(x["per100"] if x["per100"] is not None else -1), x["name"]))
    return profiles


def _by_account(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("accountId"):
            out.setdefault(str(r["accountId"]), []).append(r)
    return out


@router.get("/applications/profiles")
async def applications_profiles(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: Literal["week", "month", "custom"] = Query("month"),
    date: str = Query("", description="YYYY-MM-DD: any day in the week or month; custom: the first day"),
    to: str = Query("", description="YYYY-MM-DD: a custom range's last day"),
    region: Literal["all", "Latam", "EU", "US"] = Query("all"),
    user_id: Optional[str] = Query(None, alias="user", description="admin only: view this user"),
    scope: Literal["me", "profile", "team"] = Query("me", description="me: the subject's profiles; team: every profile"),
    profile: Optional[str] = Query(None, description="scope=profile: the profile (Account) id"),
) -> dict:
    """The "Your profiles" report (cohort = rows applied in the period) and the coverage numbers. Grades compare
    against the team = every profile of the region; `scope=team` has no per-profile list."""
    sc = await cs.conversion_scope(user, period, date, to, region, user_id, scope, profile)
    sid, start_d, end_d, start, end, now = (sc[k] for k in ("sid", "start_d", "end_d", "start", "end", "now"))
    win = await response_window(sc["today"])
    cutoff = now - timedelta(days=win["matured_days"])
    team = team_baseline(summarize(sc["teamRows"], cutoff), sc["teamRows"])
    profiles = [] if scope == "team" else await _graded_profiles(
        sc["accounts"], summarize(sc["rows"], cutoff), team, start)
    own = [r for r in sc["rows"] if r.get("bidStatus") != "rejected"]
    # header / unmatched count the subject's whole bid set: shown only for `me`, over the clamped period
    header = unmatched = None
    if scope == "me":
        header = unmatched = 0
        if end_d >= cs.DATA_START:
            regions, _ = await interview_rates._profile_regions()
            header = (await interview_rates.counts([sid], start_d, end_d, region, regions, {},
                                                   logged_only=False)).get(sid, (0,))[0]
            unmatched = await count_unmatched_interviews(start, end, sid, region, regions)
    return {
        "label": sc["label"],
        "subject": sc["subject"],
        "window": win,
        "coverage": {"rows": len(own), "headerBids": header, "noLink": sum(1 for r in own if r["key"].startswith("src:")),
                     "unmatchedInterviews": unmatched},
        "team": team,
        "profiles": profiles,
    }


@router.get("/applications/changes")
async def applications_changes(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: Literal["week", "month", "custom"] = Query("month"),
    date: str = Query("", description="YYYY-MM-DD: any day in the week or month; custom: the first day"),
    to: str = Query("", description="YYYY-MM-DD: a custom range's last day"),
    region: Literal["all", "Latam", "EU", "US"] = Query("all"),
    user_id: Optional[str] = Query(None, alias="user", description="admin only: view this user"),
    scope: Literal["me", "profile", "team"] = Query("me", description="me: the subject's profiles; team: every profile"),
    profile: Optional[str] = Query(None, description="scope=profile: the profile (Account) id"),
) -> dict:
    """"What to change": up to 3 suggestions per profile of the scope (none for `scope=team`), from the pattern each
    profile shows; team numbers from every profile of the region."""
    sc = await cs.conversion_scope(user, period, date, to, region, user_id, scope, profile)
    if scope == "team":
        return {"label": sc["label"], "subject": sc["subject"], "profiles": []}
    now, today = sc["now"], sc["today"]
    md = (await response_window(today))["matured_days"]
    cutoff = now - timedelta(days=md)
    team_rows = sc["teamRows"]
    team = team_baseline(summarize(team_rows, cutoff), team_rows)
    profiles = await _graded_profiles(sc["accounts"], summarize(sc["rows"], cutoff), team, sc["start"])
    # volume_up_rate_down compares with the previous window: off when that window reaches before the data starts
    ps, pe = previous_window(period, sc["start_d"], sc["end_d"], today)
    prev = summarize(await load_period_rows(*_bounds(ps, pe), "all"), cutoff) if ps >= cs.DATA_START else {}
    tctx = changes.team_context(team_rows, cutoff, team)
    by_acct = _by_account(sc["rows"])
    pctx = {p["accountId"]: changes.profile_context(by_acct[p["accountId"]], cutoff) for p in profiles}
    quiet: set[str] = set()
    if sc["start_d"] <= today <= sc["end_d"]:
        cands = [p["accountId"] for p in profiles if changes.quiet_candidate(by_acct[p["accountId"]], now, md)]
        busy = await accounts_with_rounds_since([PydanticObjectId(i) for i in cands],
                                                now - timedelta(days=changes.QUIET_DAYS))
        quiet = set(cands) - busy
    bidder_ids = [c["topBidderId"] for c in pctx.values() if c["topBidderId"]]
    names = {d["_id"]: d.get("name") async for d in Bidder.get_motor_collection().find(
        {"_id": {"$in": bidder_ids}}, {"name": 1})} if bidder_ids else {}
    out = []
    for p in profiles:
        pc = pctx[p["accountId"]]
        ctx = {**tctx, **pc, "matured_days": md, "prev": prev.get(p["accountId"]), "quietNow": p["accountId"] in quiet,
               "bidder": names.get(pc["topBidderId"]), "reasons": p["reasons"]}
        sug = changes.suggest(p, p, p["grade"], ctx)
        if sug:
            out.append({"accountId": p["accountId"], "name": p["name"], "grade": p["grade"], "suggestions": sug})
    return {"label": sc["label"], "subject": sc["subject"], "profiles": out}


@router.get("/applications/timing")
async def applications_timing(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: Literal["week", "month", "custom"] = Query("month"),
    date: str = Query("", description="YYYY-MM-DD: any day in the week or month; custom: the first day"),
    to: str = Query("", description="YYYY-MM-DD: a custom range's last day"),
    region: Literal["all", "Latam", "EU", "US"] = Query("all"),
    user_id: Optional[str] = Query(None, alias="user", description="admin only: view this user"),
    scope: Literal["me", "profile", "team"] = Query("me", description="me: the subject's profiles; team: every profile"),
    profile: Optional[str] = Query(None, description="scope=profile: the profile (Account) id"),
) -> dict:
    """"When to bid" (weekday, 3-hour blocks) and bidder quality; the loader returns rejected rows too, each count
    drops them. `scope=team`: you == team and no bidder table."""
    sc = await cs.conversion_scope(user, period, date, to, region, user_id, scope, profile)
    you, rows = sc["rows"], sc["teamRows"]
    win = await response_window(sc["today"])
    cutoff = sc["now"] - timedelta(days=win["matured_days"])
    tz = app_tz()
    owners = list({a["ownerId"] for a in sc["accounts"].values()})  # an admin's scope=profile: that profile's owner
    names = {} if scope == "team" or not owners else {d["_id"]: d["name"] async for d in Bidder.get_motor_collection(
    ).find({"createdBy": {"$in": owners}}, {"name": 1})}
    return {"label": sc["label"], "subject": sc["subject"], "window": win,
            "timezone": tz.key, **timing.by_time(you, rows, cutoff, tz),
            "bidders": timing.bidders(you, names, cutoff), "teamBidder": timing.team_bidder(rows, cutoff)}

