"""Daily bid review: owners of a bidder (and admins) list its bid records, view screenshots and approve or reject."""

import asyncio
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from app.bids.evidence import duration_sec, evidence_for, job_domain
from app.bids.live_counts import TtlCache
from app.bids.alerts import bid_alerts, pending_days
from app.bids.insights import insights
from app.bids.performance import day_columns, pay_week_columns, performance
from app.bids.records import _naive_utc, count_by_status, refresh_report_rows
from app.bids.schemas import _iso_utc
from app.bids.storage import BidStorageError, count_folders, get_bid_storage
from app.bids.week import PayWeek, combine_boards, pay_week, pay_weeks_touching, week_board, week_label
from app.profile_metrics import bid_report_day
from app.bids.windows import (daily_cutoff_on, latest_cutoff, next_cutoff, open_window, period_key,
                              report_day, schedule_from_settings, window_for)
from app.config import get_settings
from app.deps import get_current_user
from app.interview_rates import label, window
from app.models import UserExtra
from app.models.bid import Bid
from app.models.bid_report import BidReport
from app.models.bidder import Bidder
from app.routers._util import to_object_id
from app.routers.metrics import _stats_days

router = APIRouter(prefix="/bids", tags=["bids"])

Status = Literal["pending", "approved", "rejected"]
RejectReason = Literal["no_submission", "wrong_profile", "not_job_page", "duplicate", "incomplete", "other"]
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

# A folder listing reads the bidder's whole bucket prefix, and the review page reloads after every decision.
FOLDER_CACHE = TtlCache(timedelta(minutes=5))


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bid not found")


def _can_review(bidder: Optional[Bidder], user) -> bool:
    """The bidder's owner or an admin. Archived bidders stay reviewable."""
    return bidder is not None and (bidder.createdBy == user.id or user.role == "admin")


def _reviewable(bidder: Optional[Bidder], user) -> Bidder:
    """404 (not 403) for a missing or foreign bid, like the other bidder routes."""
    if not _can_review(bidder, user):
        raise _not_found()
    return bidder


async def _load_reviewable(bid_id: str, user) -> tuple[Bid, Bidder]:
    if not PydanticObjectId.is_valid(bid_id):
        raise _not_found()
    bid = await Bid.get(PydanticObjectId(bid_id))
    if bid is None:
        raise _not_found()
    return bid, _reviewable(await Bidder.get(bid.bidderId), user)


def _thumb_key(bid: Bid) -> Optional[str]:
    """The screenshot a list shows small: the (last) Submit one, else the last step; uploaded ones only."""
    uploaded = sorted((s for s in bid.screenshots if s.uploadedAt is not None), key=lambda s: (s.step, s.capturedAt))
    submits = [s for s in uploaded if s.trigger == "submit"]
    shot = (submits or uploaded or [None])[-1]
    return shot.key if shot else None


async def _thumb_urls(bids: list[Bid]) -> dict[str, Optional[str]]:
    """Signed thumbnail link per bid id; None when nothing is uploaded or links can't be signed (the list still loads)."""
    keys = {str(b.id): _thumb_key(b) for b in bids}
    bucket, storage = get_settings().BID_STORAGE_BUCKET, get_bid_storage()
    if not bucket or not hasattr(storage, "presign_get") or not any(keys.values()):
        return dict.fromkeys(keys)
    try:  # signing is offline but synchronous
        return await asyncio.to_thread(lambda: {i: k and storage.presign_get(bucket, k) for i, k in keys.items()})
    except BidStorageError:
        return dict.fromkeys(keys)


def _bid_out(bid: Bid, bidder_name: str, evidence: dict, thumb_url: Optional[str] = None) -> dict:
    """evidence is the bid's entry from evidence_for: profileName, resumeNames, tailoredResumeName."""
    return {
        "id": str(bid.id),
        "kind": bid.kind,
        "bidderId": str(bid.bidderId) if bid.bidderId else "self",
        "bidderName": bidder_name,
        "jobUrl": bid.jobUrl,
        "jobTitle": bid.jobTitle,
        "firstAt": _iso_utc(bid.firstAt),
        "submittedAt": _iso_utc(bid.submittedAt),
        "screenshotCount": len(bid.screenshots),
        "missingUploads": sum(1 for s in bid.screenshots if s.uploadedAt is None),
        "status": bid.status,
        "reviewedByName": bid.reviewedByName,
        "reviewedAt": _iso_utc(bid.reviewedAt),
        "note": bid.note,
        "changedSinceReview": bid.changedSinceReview,
        "jobDomain": job_domain(bid.jobUrl),
        "durationSec": duration_sec(bid),
        **evidence,
        "rejectReason": bid.rejectReason,
        "thumbUrl": thumb_url,
    }


async def _single_bid_out(bid: Bid, bidder: Optional[Bidder], self_name: str = "Self") -> dict:
    evidence = (await evidence_for([bid], {str(bidder.id): bidder} if bidder else {}))[str(bid.id)]
    return _bid_out(bid, bidder.name if bidder else self_name, evidence, (await _thumb_urls([bid]))[str(bid.id)])


async def _self_name(owner_id, user) -> str:
    """How a self bid is labelled: "Self" for the viewer's own, "Self · <owner>" for an admin looking at another's."""
    if owner_id == user.id:
        return "Self"
    owner = await UserExtra.get(owner_id)
    return f"Self · {(owner.name or owner.email) if owner else 'owner'}"


async def _load_viewable(bid_id: str, user) -> tuple[Bid, Optional[Bidder]]:
    """A bid to look at: a bidder's bid as for review; a self bid (no bidder) for its owner and admins (read-only:
    the review routes keep refusing it)."""
    if not PydanticObjectId.is_valid(bid_id):
        raise _not_found()
    bid = await Bid.get(PydanticObjectId(bid_id))
    if bid is None:
        raise _not_found()
    if bid.kind == "self":
        if bid.ownerId != user.id and user.role != "admin":
            raise _not_found()
        return bid, None
    return bid, _reviewable(await Bidder.get(bid.bidderId), user)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decision(user, new_status: str, note: Optional[str], reason: Optional[str] = None) -> dict:
    """Fields of one decision. Clearing changedSinceReview in the same update keeps a screenshot that arrives
    mid-review from being flagged and unflagged out of order. The reject reason is kept only for a rejection."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "status": new_status, "reviewedBy": user.id, "reviewedByName": user.name or user.email or "",
        "reviewedAt": now, "note": note, "rejectReason": reason if new_status == "rejected" else None,
        "changedSinceReview": False, "updatedAt": now,
    }


def _check_reason(new_status: str, reason: Optional[str], note: Optional[str]) -> None:
    """A reason belongs to a rejection, and Other has to be explained in the note."""
    if reason is not None and new_status != "rejected":
        raise HTTPException(status_code=400, detail="A reason only goes with a rejection")
    if reason == "other" and not note:
        raise HTTPException(status_code=400, detail="A note is required for Other")


# Complete = ready to approve without a look at the gaps: still pending, Submit captured, no unconfirmed upload.
_COMPLETE = {
    "status": "pending",
    "submittedAt": {"$ne": None},
    "screenshots": {"$not": {"$elemMatch": {"uploadedAt": None}}},
}


@router.get("")
async def list_bids(
    user: Annotated[UserExtra, Depends(get_current_user)],
    day: Optional[str] = None,
    bidderId: Optional[str] = None,
    status: Optional[Status] = None,
    week: Optional[str] = None,
    self_: bool = Query(False, alias="self"),
    ownerId: Optional[str] = None,
    accountId: Optional[str] = None,
) -> dict:
    sched, now = schedule_from_settings(), _now()
    if day is not None and week is not None:
        raise HTTPException(status_code=400, detail="use day or week, not both")
    pw = _pay_week_param(week, sched, now) if week is not None else None
    if pw is not None:
        cutoff = pw.cutoff
    elif day is None:
        # The open window: the one that ends at the next daily cutoff, so bids filed after the report are listed.
        opened, _ = open_window("daily", now, sched)
        cutoff = next_cutoff("daily", opened, sched)
    else:
        try:
            if not _DAY.fullmatch(day):
                raise ValueError(day)
            cutoff = daily_cutoff_on(date.fromisoformat(day), sched)
            window_for("daily", cutoff, sched)  # overflows at the edges of the calendar
        except (ValueError, OverflowError):
            raise HTTPException(status_code=400, detail="Invalid day") from None
    start, end = (pw.start, pw.end) if pw is not None else window_for("daily", cutoff, sched)
    if self_:
        return await _self_listing(user, ownerId, status, start, end, cutoff, pw, sched, accountId)

    candidates = await _visible_bidders(user)
    if bidderId is not None:
        candidates = [b for b in candidates if str(b.id) == bidderId]
        if not candidates:
            raise HTTPException(status_code=404, detail="bidder not found")
    counts = await count_by_status([b.id for b in candidates], start, end)
    # An archived bidder shows only on days it has records (they are kept and reviewable).
    shown = [b for b in candidates if b.archivedAt is None or sum(counts[str(b.id)])]
    names = {b.id: b.name for b in shown}

    query: dict = {"bidderId": {"$in": list(names)}, "firstAt": {"$gte": _naive_utc(start), "$lt": _naive_utc(end)}}
    if status:
        query["status"] = status
    bids = await Bid.find(query).sort("-firstAt").to_list()
    evidence = await evidence_for(bids, {str(b.id): b for b in shown})
    thumbs = await _thumb_urls(bids)

    folders = await _folders_without_record(shown, counts, start, end, now)
    return {
        "day": None if pw is not None else period_key(cutoff),
        "week": period_key(pw.cutoff) if pw is not None else None,
        "start": _iso_utc(start),
        "end": _iso_utc(end),
        "timezone": sched.tz.key,
        "bids": [_bid_out(b, names[b.bidderId], evidence[str(b.id)], thumbs[str(b.id)]) for b in bids],
        "summary": {
            str(b.id): dict(
                zip(("name", "approved", "pending", "rejected"), (b.name, *counts[str(b.id)])),
                foldersWithoutRecord=folders[str(b.id)],
            )
            for b in shown
        },
    }


async def _self_listing(user, owner_id: Optional[str], status, start, end, cutoff, pw, sched,
                        account_id: Optional[str] = None) -> dict:
    """The owners' own applies (self bids) of a day or pay week: staff see their own; an admin one owner's (ownerId)
    or everyone's. `accountId` narrows to one profile's. Same shape as the bidders' listing; never reviewable."""
    if user.role == "admin":
        owners = [to_object_id(owner_id)] if owner_id else None
    else:
        owners = [] if owner_id and owner_id != str(user.id) else [user.id]
    query: dict = {"kind": "self", "firstAt": {"$gte": _naive_utc(start), "$lt": _naive_utc(end)}}
    if owners is not None:
        query["ownerId"] = {"$in": owners}
    if account_id:
        query["accountId"] = to_object_id(account_id)
    if status:
        query["status"] = status
    bids = await Bid.find(query).sort("-firstAt").to_list() if owners != [] else []
    evidence = await evidence_for(bids, {})
    thumbs = await _thumb_urls(bids)
    names = {o: await _self_name(o, user) for o in {b.ownerId for b in bids}}
    return {
        "day": None if pw is not None else period_key(cutoff),
        "week": period_key(pw.cutoff) if pw is not None else None,
        "start": _iso_utc(start),
        "end": _iso_utc(end),
        "timezone": sched.tz.key,
        "bids": [_bid_out(b, names[b.ownerId], evidence[str(b.id)], thumbs[str(b.id)]) for b in bids],
        "summary": {},
    }


async def _folders_without_record(
    bidders: list[Bidder], counts: dict, start: datetime, end: datetime, now: datetime
) -> dict[str, Optional[int]]:
    """Job folders in Backblaze for the window minus the bidder's bids that day (those still on the old extension
    leave folders with no record); None when the folder can't be read."""
    found: dict[str, Optional[int]] = {}
    misses: list[tuple[str, str]] = []
    for b in bidders:
        hit = FOLDER_CACHE.get((str(b.id), b.screenshotFolderUrl, start.isoformat()), now)
        if hit is None:
            misses.append((str(b.id), b.screenshotFolderUrl))
        else:
            found[str(b.id)] = hit
    for bidder_id, (n, _error) in (await count_folders(misses, start, end, get_bid_storage())).items():
        found[bidder_id] = n
        if n is not None:  # a failure is retried on the next request
            bidder = next(b for b in bidders if str(b.id) == bidder_id)
            FOLDER_CACHE.set((bidder_id, bidder.screenshotFolderUrl, start.isoformat()), n, now)
    return {i: None if n is None else max(0, n - sum(counts[i])) for i, n in found.items()}


def _pay_week_param(week: Optional[str], sched, now: datetime) -> PayWeek:
    """The pay week holding the ?week= date (the current one when None); 400 for a malformed or out-of-range date."""
    try:
        if week is None:
            return pay_week(sched, now)
        if not _DAY.fullmatch(week):
            raise ValueError(week)
        return pay_week(sched, now, date.fromisoformat(week))
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail="invalid week") from None


class BidPeriod(BaseModel):
    """A Bidders page period in report days: `start`..`end` (end clipped to today), its label and columns; `pay_week`
    for a week (weekly cutoff to weekly cutoff); `today` = the open report day (after the daily cutoff, tomorrow's)."""
    model_config = {"arbitrary_types_allowed": True}
    period: str
    start: date
    end: date
    label: str
    today: date
    columns: list[dict]
    pay_week: Optional[PayWeek] = None
    pay_weeks: list[PayWeek] = []  # span 2: the two pay weeks, earlier first (their money comes from the week boards)
    span: int = 1  # pay weeks in a `week` period: 1, or 2 (the anchored pay week and the one before; no pay_week board)


def bid_period(period: str, day: str, to: str, now: datetime, weeks: int = 1) -> BidPeriod:
    """The period the Bidders page asked for: a pay week (the one holding `day`, default now) with a column per report
    day, or a calendar month / custom range of report days with a column per pay week. `weeks=2` (week only) is that
    pay week and the one before, with a column per pay week like a custom range. Raises 422 / 400 like the statistics
    periods."""
    if period not in ("week", "month", "custom"):
        raise HTTPException(status_code=422, detail="period must be week, month or custom")
    sched = schedule_from_settings()
    today = bid_report_day(now.astimezone(timezone.utc).replace(tzinfo=None), sched)
    if period == "week":
        try:
            anchor = date.fromisoformat(day) if day else None
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid date") from None
        pw = pay_week(sched, now, anchor)
        days = [date.fromisoformat(d.key) for d in pw.days]
        if weeks == 2:
            earlier = pay_week(sched, now, days[0] - timedelta(days=1))
            first = date.fromisoformat(earlier.days[0].key)
            end = min(days[-1], today)
            return BidPeriod(period=period, start=first, end=end, label=label("custom", first, days[-1]), today=today,
                             columns=pay_week_columns(first, end, sched) if first <= end else [], span=2,
                             pay_weeks=[earlier, pw])
        return BidPeriod(period=period, start=days[0], end=min(days[-1], today), label=week_label(pw), today=today,
                         columns=day_columns(days), pay_week=pw)
    first, until = _stats_days(period, day or today.isoformat(), to)
    start, window_end = window(period, first, until)
    end = min(window_end, today)
    return BidPeriod(period=period, start=start, end=end, label=label(period, start, window_end), today=today,
                     columns=pay_week_columns(start, end, sched) if start <= end else [])


async def _self_rows(user, owner, bidders: list[Bidder], p: BidPeriod) -> list[dict]:
    """The owners' own applies recorded by the extension (self bids), one row per owner in view: the signed-in owner,
    the picked owner, or every owner with bidders for an admin viewing all. Kept out of the bidders' totals."""
    from app.models import Account
    from app.profile_metrics import interview_rounds
    from app.self_bids import tracked_counts

    if owner is not None:
        owners = [owner]
    elif user.role != "admin":
        owners = [user.id]
    else:
        owners = list({b.createdBy for b in bidders})
    if not owners or p.start > p.end:
        return []
    names = {u.id: u.name or u.email or "" for u in await UserExtra.find({"_id": {"$in": owners}}).to_list()}
    col_of = {}
    for i, c in enumerate(p.columns):
        d = date.fromisoformat(c["start"])
        while d <= date.fromisoformat(c["end"]):
            col_of[d] = i
            d += timedelta(days=1)
    out = []
    for o in owners:
        counts = await tracked_counts(o, p.start, p.end)
        if not counts:
            continue
        # One sub-row per profile the owner applied with in the period; the row is their sum.
        by_profile: dict[str, list[int]] = {}
        for (pid, d), n in counts.items():
            if d in col_of:
                by_profile.setdefault(pid, [0] * len(p.columns))[col_of[d]] += n
        weekly = [sum(col) for col in zip(*by_profile.values())] if by_profile else [0] * len(p.columns)
        accounts = [a.id for a in await Account.find({"createdBy": o}).to_list()]
        rounds = await interview_rounds(accounts, p.start, p.end) if accounts else {}
        # Rounds of interviews tagged "Applied by: Self" (index 2), per profile.
        self_rounds: dict[str, int] = defaultdict(int)
        for (acc, _day), v in rounds.items():
            self_rounds[acc] += v[2]
        profile_names = {
            str(a.id): a.name or "Unnamed profile"
            for a in await Account.find({"_id": {"$in": [to_object_id(pid) for pid in by_profile]}}).to_list()
        }
        profiles = sorted(
            ({"accountId": pid, "name": profile_names.get(pid, "Unknown profile"), "total": sum(cells),
              "weekly": cells, "interviews": self_rounds.get(pid, 0)} for pid, cells in by_profile.items()),
            key=lambda r: (r["name"].lower(), r["accountId"]),
        )
        last = await Bid.find({"kind": "self", "ownerId": o}).sort("-firstAt").limit(1).to_list()
        out.append({
            "ownerId": str(o), "ownerName": names.get(o, ""), "total": sum(counts.values()), "weekly": weekly,
            "interviews": sum(self_rounds.values()),
            "lastBidAt": _iso_utc(last[0].firstAt) if last else None,
            "profiles": profiles,
        })
    return out


def _pay_week_out(pw: PayWeek, now: datetime) -> dict:
    sched = schedule_from_settings()
    return {"start": _iso_utc(pw.start), "end": _iso_utc(pw.end), "label": week_label(pw), "timezone": sched.tz.key,
            "days": [{"day": d.key, "isToday": d.start <= now < d.end, "isFuture": d.start > now} for d in pw.days]}


async def _visible_bidders(user, owner_id: Optional[PydanticObjectId] = None) -> list[Bidder]:
    """The user's bidders (all of them for an admin), archived included, by name. `owner_id` narrows to one
    owner's bidders; for staff it can only narrow their own list, never widen it."""
    scope = {} if user.role == "admin" else {"createdBy": user.id}
    if owner_id is not None:
        if "createdBy" in scope and scope["createdBy"] != owner_id:
            return []
        scope["createdBy"] = owner_id
    return await Bidder.find(scope).sort("name").to_list()


@router.get("/week")
async def bids_week(
    user: Annotated[UserExtra, Depends(get_current_user)],
    week: Optional[str] = None,
    includeArchived: bool = False,
    ownerId: Optional[str] = None,
) -> dict:
    """The pay-week board: each bidder's approved / pending / rejected per day, and the week's totals and pay.
    `ownerId` limits it to one user's bidders (the admin owner filter)."""
    sched, now = schedule_from_settings(), _now()
    pw = _pay_week_param(week, sched, now)
    owner = to_object_id(ownerId) if ownerId else None
    bidders = [b for b in await _visible_bidders(user, owner) if includeArchived or b.archivedAt is None]
    return await week_board(bidders, pw, now, sched, get_bid_storage(), FOLDER_CACHE)


@router.get("/performance")
async def bids_performance(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: str = Query("month"),
    date: str = Query(""),
    to: str = Query(""),
    weeks: Annotated[int, Query(ge=1, le=2)] = 1,
    includeArchived: bool = False,
    ownerId: Optional[str] = None,
) -> dict:
    """Bidder performance for a pay week (`weeks=2`: that and the one before), month or custom range: bids, hit days,
    streaks, interviews and rankings. The current period runs to today's open report day. `ownerId` limits it to one
    user's bidders."""
    now = _now()
    p = bid_period(period, date, to, now, weeks)
    owner = to_object_id(ownerId) if ownerId else None
    bidders = [b for b in await _visible_bidders(user, owner) if includeArchived or b.archivedAt is None]
    out = await performance(bidders, period, p.start, p.end, p.label, p.today, columns=p.columns, span=p.span)
    out["payWeek"] = _pay_week_out(p.pay_week, now.astimezone(timezone.utc)) if p.pay_week else None
    out["selfRows"] = await _self_rows(user, owner, bidders, p)
    if p.pay_week:
        # Money and "to review" come from the pay week board (the pay-week window, folder counts before records start),
        # so they match /bids/week, the weekly report and actual pay; the performance measures stay on report days.
        board = await week_board(bidders, p.pay_week, now, schedule_from_settings(), get_bid_storage(), FOLDER_CACHE)
        cells = {r["id"]: r for r in board["bidders"]}
        for row in out["bidders"]:
            if row["id"] in cells:
                row["dayCells"], row["week"] = cells[row["id"]]["days"], cells[row["id"]]["week"]
        t = board["totals"]
        out["totals"].update({k: t[k] for k in ("approved", "pending", "rejected", "toReview", "pay", "days")})
        today = next((d for d in out["payWeek"]["days"] if d["isToday"]), None)
        cell = next((d for d in t["days"] if today and d["day"] == today["day"]), None)
        out["totals"]["today"] = {"day": cell["day"], "approved": cell["approved"], "pending": cell["pending"]} if cell else None
    elif p.pay_weeks:
        # Two pay weeks: money and "to review" are the sum of the two pay weeks' boards, exactly what each one reports
        # (pay-week windows, folder counts before records start), not the report-day sums, which run past the Saturday
        # 16:00 close and miss folder-counted days. The performance measures stay on report days.
        sched, storage = schedule_from_settings(), get_bid_storage()
        boards = [await week_board(bidders, pw, now, sched, storage, FOLDER_CACHE) for pw in p.pay_weeks]
        money = combine_boards(boards)
        for row in out["bidders"]:
            if row["id"] in money["blocks"]:
                row["week"] = money["blocks"][row["id"]]
        out["totals"].update(money["totals"])
    return out


@router.get("/insights")
async def bids_insights(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: str = Query("month"),
    date: str = Query(""),
    to: str = Query(""),
    weeks: Annotated[int, Query(ge=1, le=2)] = 1,
    ownerId: Optional[str] = None,
) -> dict:
    """The Bidders page card: one owner's active bidders vs the platform over the Performance tab's period, and each
    measure's best bidder vs the best on the platform. Staff: their own bidders; an admin: one owner's (`ownerId`),
    else nothing to compare."""
    p = bid_period(period, date, to, _now(), weeks)
    start, end, text, today = p.start, p.end, p.label, p.today
    if user.role == "admin" and not ownerId:
        return {"start": start.isoformat(), "end": end.isoformat(), "label": text, "mine": None, "platform": None,
                "tops": None}
    owner = to_object_id(ownerId) if ownerId else None
    return await insights(await _visible_bidders(user, owner), period, start, end, text, today, columns=p.columns, span=p.span)


@router.get("/alerts")
async def bids_alerts(
    user: Annotated[UserExtra, Depends(get_current_user)],
    ownerId: Optional[str] = None,
) -> dict:
    """The Bidders page banners: one owner's bidders that need action and archive suggestions, over fixed windows
    ending today (report timezone). Staff: their own bidders; an admin: one owner's (`ownerId`), else none."""
    now = _now()
    today = bid_report_day(now.astimezone(timezone.utc).replace(tzinfo=None), schedule_from_settings())
    if user.role == "admin" and not ownerId:
        return {"asOf": today.isoformat(), "flags": None, "archive": None}
    owner = to_object_id(ownerId) if ownerId else None
    return await bid_alerts(await _visible_bidders(user, owner), today, now)


@router.get("/clock")
async def bids_clock(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """"Now" as the report schedule sees it, so the client never hard-codes the schedule: the open report day, the
    first and last report days of the pay week holding now (7 days; at the switch 8, or 1 with the short week) and the
    first report day of the pay week before it. Any signed-in user."""
    sched, now = schedule_from_settings(), _now()
    week = pay_week(sched, now)
    previous = pay_week(sched, week.start - timedelta(microseconds=1))
    return {
        "today": report_day(now, sched).isoformat(),
        "payWeekStart": week.days[0].key,
        "payWeekEnd": week.days[-1].key,
        "previousPayWeekStart": previous.days[0].key,
        "timezone": sched.tz.key,
    }


def _parse_day(value: str, what: str = "date") -> date:
    """A YYYY-MM-DD day; 400 for anything else (a missing day, 2026-02-31, a basic-format date)."""
    try:
        if not _DAY.fullmatch(value):
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {what}") from None


def _parse_since(value: str) -> datetime:
    """An ISO moment (aware, `Z`, or naive = UTC) as naive UTC, the way reviewedAt is stored; 400 when malformed."""
    try:
        return _naive_utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00")))
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail="invalid since") from None


def _pending_window(period: str, day: str, to: str, weeks: int, now: datetime, sched) -> tuple[str, Optional[tuple[datetime, datetime]]]:
    """(label, (start, end) of the firstAt window) of a review scope; the window is None for no time limit and
    start == end for a period that has not begun (nothing to list)."""
    if period == "all":
        return "All pending", None
    try:
        if period == "day":
            d = _parse_day(day) if day else report_day(now, sched)
            return f"{d:%a} {d:%b} {d.day}", window_for("daily", daily_cutoff_on(d, sched), sched)
        for value, what in ((day, "date"), (to, "to")):
            if value:
                _parse_day(value, what)
        p = bid_period(period, day, to, now, weeks)
        if p.period == "week":
            ws = p.pay_weeks or [p.pay_week]
            return p.label, (ws[0].start, ws[-1].end)
        if p.start > p.end:  # a month that has not begun
            return p.label, (now, now)
        first, _ = window_for("daily", daily_cutoff_on(p.start, sched), sched)
        _, last = window_for("daily", daily_cutoff_on(p.end, sched), sched)
        return p.label, (first, last)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail="invalid date") from None


@router.get("/pending")
async def pending_bids(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: Literal["all", "day", "week", "month", "custom"] = "all",
    date: str = "",
    to: str = "",
    weeks: Annotated[int, Query(ge=1, le=2)] = 1,
    ownerId: Optional[str] = None,
    bidderId: Optional[str] = None,
    since: Optional[str] = None,
) -> dict:
    """The bids one review scope lists, oldest first: `all` pending bids of any age, a report `day`, a pay `week`
    (`weeks=2`: that and the one before), a `month` or a `custom` range of report days (period rules as
    /bids/performance). `since` (ISO) also keeps any bid decided at or after it, so a bid reviewed while the overlay
    is open stays listed. Staff: their own bidders; an admin: one owner's (`ownerId`) or everyone's. Archived
    bidders are included; `bidderId` narrows to one bidder (404 outside the visible ones)."""
    sched, now = schedule_from_settings(), _now()
    since_at = _parse_since(since) if since else None
    text, window_ = _pending_window(period, date, to, weeks, now, sched)
    owner = to_object_id(ownerId) if ownerId else None
    candidates = await _visible_bidders(user, owner)
    if bidderId is not None:
        candidates = [b for b in candidates if str(b.id) == bidderId]
        if not candidates:
            raise HTTPException(status_code=404, detail="bidder not found")
    out = {"label": text, "timezone": sched.tz.key, "bidderName": candidates[0].name if bidderId is not None else None}
    if not candidates or (window_ is not None and window_[0] >= window_[1]):
        return {**out, "bids": []}
    query: dict = {"bidderId": {"$in": [b.id for b in candidates]}}
    if window_ is not None:
        query["firstAt"] = {"$gte": _naive_utc(window_[0]), "$lt": _naive_utc(window_[1])}
    query["$or"] = [{"status": "pending"}] + ([{"reviewedAt": {"$gte": since_at}}] if since_at else [])
    bids = await Bid.find(query).sort([("firstAt", 1), ("_id", 1)]).to_list()
    names = {b.id: b.name for b in candidates}
    evidence = await evidence_for(bids, {str(b.id): b for b in candidates})
    thumbs = await _thumb_urls(bids)
    return {**out, "bids": [_bid_out(b, names[b.bidderId], evidence[str(b.id)], thumbs[str(b.id)]) for b in bids]}


@router.get("/backlog")
async def bids_backlog(
    user: Annotated[UserExtra, Depends(get_current_user)],
    ownerId: Optional[str] = None,
) -> dict:
    """The To review counts: every pending bid of any age, per bidder (archived bidders included, the set
    /bids/pending walks). Staff: their own bidders; an admin: one owner's (`ownerId`) or everyone's. Bidders with
    nothing pending are left out; most pending first, then by name."""
    sched, now = schedule_from_settings(), _now()
    today = report_day(now, sched)
    owner = to_object_id(ownerId) if ownerId else None
    bidders = await _visible_bidders(user, owner)
    days = await pending_days([b.id for b in bidders], sched)
    owners = {b.createdBy for b in bidders if str(b.id) in days}
    names = {u.id: u.name or u.email or "" for u in await UserExtra.find({"_id": {"$in": list(owners)}}).to_list()} if owners else {}
    rows = [{"bidderId": str(b.id), "name": b.name, "ownerName": names.get(b.createdBy), "pending": len(days[str(b.id)]),
             "oldestDay": min(days[str(b.id)]).isoformat()} for b in bidders if days.get(str(b.id))]
    rows.sort(key=lambda r: (-r["pending"], r["name"].lower()))
    return {"today": today.isoformat(), "total": sum(r["pending"] for r in rows),
            "todayCount": sum(d == today for v in days.values() for d in v),
            "oldestDay": min((r["oldestDay"] for r in rows), default=None), "bidders": rows}


def _pay_bidder(bidder_id, name, country, profile, owner_id, owner_name, approved, pending, rate, pay, error) -> dict:
    return {"bidderId": str(bidder_id), "name": name, "country": country, "profileName": profile,
            "ownerId": str(owner_id), "ownerName": owner_name, "approved": approved, "pending": pending, "rate": rate,
            "pay": pay, "error": error}


@router.get("/pay")
async def bids_pay(
    user: Annotated[UserExtra, Depends(get_current_user)],
    period: str = Query("week"),
    date: str = Query(""),
    to: str = Query(""),
    weeks: Annotated[int, Query(ge=1, le=2)] = 1,
    ownerId: Optional[str] = None,
) -> dict:
    """Each pay week's pay per bidder, newest week first, for a pay week (`weeks=2`: that and the one before), a month
    or a custom range (every pay week touching it). A closed week is what its weekly report stored (so a later rate
    edit never changes it; `source` "report"), falling back to the live board when nothing was stored; the open week is
    the live board. Staff: their own bidders; an admin: one owner's (`ownerId`) or everyone's. Archived bidders are
    listed only with approved or pending bids."""
    sched, now = schedule_from_settings(), _now()
    p = bid_period(period, date, to, now, weeks)
    if p.pay_week:
        pws = [p.pay_week]
    elif p.pay_weeks:
        pws = list(reversed(p.pay_weeks))
    else:
        pws = pay_weeks_touching(sched, now, p.start, p.end)
    owner = to_object_id(ownerId) if ownerId else None
    visible = await _visible_bidders(user, owner)
    by_id = {b.id: b for b in visible}
    if user.role != "admin":
        owner_scope = {user.id} if owner in (None, user.id) else set()
    else:
        owner_scope = {owner} if owner else None
    owner_ids = {b.createdBy for b in visible}
    reports: dict[str, list[BidReport]] = {}
    for pw in pws:
        if pw.end <= now:
            query: dict = {"kind": "weekly", "periodKey": period_key(pw.cutoff)}
            if owner_scope is not None:
                query["ownerId"] = {"$in": list(owner_scope)}
            reports[period_key(pw.cutoff)] = await BidReport.find(query).to_list() if owner_scope != set() else []
            owner_ids.update(r.ownerId for r in reports[period_key(pw.cutoff)])
    names = {u.id: u.name for u in await UserExtra.find({"_id": {"$in": list(owner_ids)}}).to_list()} if owner_ids else {}

    out_weeks = []
    for pw in pws:
        key, closed = period_key(pw.cutoff), pw.end <= now
        rows = reports.get(key, []) if closed else []
        if rows:
            source, bidders = "report", []
            for r in rows:
                bidder = by_id.get(r.bidderId)
                if bidder is not None and bidder.archivedAt is not None and not (r.count or 0) and not (r.pending or 0):
                    continue
                bidders.append(_pay_bidder(r.bidderId, r.bidderName, r.country, r.profileName, r.ownerId,
                                           names.get(r.ownerId), r.count, r.pending, r.rate, r.amount, r.error))
        else:
            source, bidders = "live", []
            board = await week_board(visible, pw, now, sched, get_bid_storage(), FOLDER_CACHE) if visible else None
            for r in board["bidders"] if board else []:
                w = r["week"]
                if r["status"] == "archived" and not (w["approved"] or 0) and not (w["pending"] or 0):
                    continue
                bidders.append(_pay_bidder(r["id"], r["name"], r["country"], r["profileName"], r["ownerId"],
                                           r["ownerName"], w["approved"], w["pending"], r["rate"], w["pay"], w["error"]))
        bidders.sort(key=lambda b: ((b["ownerName"] or "").lower(), b["name"].lower()))
        pays = [b["pay"] for b in bidders if b["pay"] is not None]
        out_weeks.append({
            "weekEnd": key, "label": week_label(pw), "end": _iso_utc(pw.end), "first": pw.days[0].key,
            "last": pw.days[-1].key, "state": "closed" if closed else "open", "source": source, "bidders": bidders,
            "totals": {"approved": sum(b["approved"] or 0 for b in bidders), "pending": sum(b["pending"] or 0 for b in bidders),
                       "pay": round(sum(pays), 2), "errors": sum(1 for b in bidders if b["error"])},
        })
    return {"label": p.label, "timezone": sched.tz.key,
            "first": min((w["first"] for w in out_weeks), default=p.start.isoformat()),
            "last": max((w["last"] for w in out_weeks), default=p.end.isoformat()), "weeks": out_weeks}


@router.get("/{bid_id}")
async def get_bid(bid_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    bid, bidder = await _load_viewable(bid_id, user)
    return await _single_bid_out(bid, bidder, await _self_name(bid.ownerId, user) if bidder is None else "Self")


@router.get("/{bid_id}/screenshots")
async def bid_screenshots(bid_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> list[dict]:
    bid, _ = await _load_viewable(bid_id, user)
    shots = sorted(bid.screenshots, key=lambda s: (s.step, s.capturedAt))
    uploaded = [s for s in shots if s.uploadedAt is not None]
    urls: dict[str, str] = {}
    if uploaded:  # an unconfirmed upload has no object to link to
        unavailable = HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Screenshots are not available")
        bucket, storage = get_settings().BID_STORAGE_BUCKET, get_bid_storage()
        if not bucket or not hasattr(storage, "presign_get"):
            raise unavailable
        try:  # signing is offline but synchronous; keep the event loop free like the upload route
            signed = await asyncio.to_thread(lambda: [storage.presign_get(bucket, s.key) for s in uploaded])
        except BidStorageError:
            raise unavailable from None
        urls = {s.key: url for s, url in zip(uploaded, signed)}
    return [
        {
            "key": s.key, "step": s.step, "trigger": s.trigger, "isSubmit": s.trigger == "submit",
            "capturedAt": _iso_utc(s.capturedAt), "url": urls.get(s.key),
            "attachments": [a.model_dump() for a in s.attachments],
        }
        for s in shots
    ]


class ReviewIn(BaseModel):
    status: Status
    note: Optional[str] = Field(default=None, max_length=500)
    reason: Optional[RejectReason] = None

    @field_validator("note")
    @classmethod
    def _note(cls, v: Optional[str]) -> Optional[str]:
        return (v or "").strip() or None


class BulkReviewIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    status: Status
    reason: Optional[RejectReason] = None
    onlyComplete: bool = False


@router.put("/{bid_id}/review")
async def review_bid(bid_id: str, body: ReviewIn, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    bid, bidder = await _load_reviewable(bid_id, user)
    _check_reason(body.status, body.reason, body.note)
    await Bid.get_motor_collection().update_one(
        {"_id": bid.id}, {"$set": _decision(user, body.status, body.note, body.reason)}
    )
    bid = await Bid.get(bid.id)
    # Write first, then recount. The recount is not atomic with other reviews: two reviews of the same day
    # finishing together can each count before the other's write, leaving the stored row one decision behind
    # until the next review of that day.
    await refresh_report_rows(bid)
    return await _single_bid_out(bid, bidder)


@router.post("/review")
async def review_bids(body: BulkReviewIn, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Decide many bids at once: every id is access-checked; foreign, unknown and malformed ids are skipped. With
    onlyComplete (approving only) a bid is also skipped unless it is complete when the write happens."""
    _check_reason(body.status, body.reason, None)  # a bulk decision has no note, so Other is refused
    if body.onlyComplete and body.status != "approved":
        raise HTTPException(status_code=400, detail="onlyComplete only goes with approved")
    wanted = set(body.ids)
    ids = [PydanticObjectId(i) for i in wanted if PydanticObjectId.is_valid(i)]
    bids = await Bid.find({"_id": {"$in": ids}}).to_list()
    bidders = {b.id: b for b in await Bidder.find({"_id": {"$in": list({b.bidderId for b in bids})}}).to_list()}
    allowed = [b for b in bids if _can_review(bidders.get(b.bidderId), user)]
    only = _COMPLETE if body.onlyComplete else {}
    if allowed and only:  # narrow to the complete ones now so the recount below skips untouched bidders and days
        allowed = await Bid.find({"_id": {"$in": [b.id for b in allowed]}, **only}).to_list()
    updated = 0
    if allowed:
        # The completeness test rides along in the write itself: a bid that changed since the read is not touched.
        result = await Bid.get_motor_collection().update_many(
            {"_id": {"$in": [b.id for b in allowed]}, **only},
            {"$set": _decision(user, body.status, None, body.reason)},
        )
        updated = result.matched_count
        # One recount per bidder, daily window and weekly window, after all the writes (same race as the single
        # review). refresh_report_rows takes both rows from one bid's firstAt; the weekly cutoff falls inside a
        # daily window (Saturday's; Monday's after the switch), so bids of one day can belong to two weekly rows.
        sched = schedule_from_settings()
        per_period: dict[tuple, Bid] = {}
        for b in allowed:
            at = b.firstAt.replace(tzinfo=timezone.utc)
            key = (b.bidderId, latest_cutoff("daily", at, sched), latest_cutoff("weekly", at, sched))
            per_period.setdefault(key, b)
        for b in per_period.values():
            await refresh_report_rows(b)
    return {"updated": updated, "skipped": len(wanted) - updated}
