"""Daily bid review: owners of a bidder (and admins) list its bid records, view screenshots and approve or reject."""

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.bids.live_counts import TtlCache
from app.bids.records import _naive_utc, count_by_status, refresh_report_rows
from app.bids.schemas import _iso_utc
from app.bids.storage import BidStorageError, count_folders, get_bid_storage
from app.bids.windows import BidSchedule, latest_cutoff, open_window, period_key, schedule_from_settings, window_for
from app.config import get_settings
from app.deps import get_current_user
from app.models import UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder

router = APIRouter(prefix="/bids", tags=["bids"])

Status = Literal["pending", "approved", "rejected"]
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


def _bid_out(bid: Bid, bidder_name: str) -> dict:
    return {
        "id": str(bid.id),
        "bidderId": str(bid.bidderId),
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
    }


def _next_daily_cutoff(at: datetime, sched: BidSchedule) -> datetime:
    """The cutoff that closes the daily report window holding `at`, in the report timezone."""
    latest = latest_cutoff("daily", at, sched)
    return datetime.combine(latest.date() + timedelta(days=1), sched.daily_at, tzinfo=sched.tz)


def _decision(user, new_status: str, note: Optional[str]) -> dict:
    """Fields of one decision. Clearing changedSinceReview in the same update keeps a screenshot that arrives
    mid-review from being flagged and unflagged out of order."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "status": new_status, "reviewedBy": user.id, "reviewedByName": user.name or user.email or "",
        "reviewedAt": now, "note": note, "changedSinceReview": False, "updatedAt": now,
    }


@router.get("")
async def list_bids(
    user: Annotated[UserExtra, Depends(get_current_user)],
    day: Optional[str] = None,
    bidderId: Optional[str] = None,
    status: Optional[Status] = None,
) -> dict:
    sched, now = schedule_from_settings(), datetime.now(timezone.utc)
    if day is None:  # the open daily window; its report is the one posted at the next cutoff
        cutoff = _next_daily_cutoff(open_window("daily", now, sched)[0], sched)
    else:
        try:
            if not _DAY.fullmatch(day):
                raise ValueError(day)
            cutoff = datetime.combine(date.fromisoformat(day), sched.daily_at, tzinfo=sched.tz)
            window_for("daily", cutoff, sched)  # overflows at the edges of the calendar
        except (ValueError, OverflowError):
            raise HTTPException(status_code=400, detail="Invalid day") from None
    start, end = window_for("daily", cutoff, sched)

    scope = {} if user.role == "admin" else {"createdBy": user.id}
    candidates = await Bidder.find(scope).sort("name").to_list()
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

    folders = await _folders_without_record(shown, counts, start, end, now)
    return {
        "day": period_key(cutoff),
        "start": _iso_utc(start),
        "end": _iso_utc(end),
        "bids": [_bid_out(b, names[b.bidderId]) for b in bids],
        "summary": {
            str(b.id): dict(
                zip(("name", "approved", "pending", "rejected"), (b.name, *counts[str(b.id)])),
                foldersWithoutRecord=folders[str(b.id)],
            )
            for b in shown
        },
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


@router.get("/{bid_id}/screenshots")
async def bid_screenshots(bid_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> list[dict]:
    bid, _ = await _load_reviewable(bid_id, user)
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
        {"key": s.key, "step": s.step, "trigger": s.trigger, "capturedAt": _iso_utc(s.capturedAt), "url": urls.get(s.key)}
        for s in shots
    ]


class ReviewIn(BaseModel):
    status: Status
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _note(cls, v: Optional[str]) -> Optional[str]:
        return (v or "").strip() or None


class BulkReviewIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    status: Status


@router.put("/{bid_id}/review")
async def review_bid(bid_id: str, body: ReviewIn, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    bid, bidder = await _load_reviewable(bid_id, user)
    await Bid.get_motor_collection().update_one({"_id": bid.id}, {"$set": _decision(user, body.status, body.note)})
    bid = await Bid.get(bid.id)
    # Write first, then recount. The recount is not atomic with other reviews: two reviews of the same day
    # finishing together can each count before the other's write, leaving the stored row one decision behind
    # until the next review of that day.
    await refresh_report_rows(bid)
    return _bid_out(bid, bidder.name)


@router.post("/review")
async def review_bids(body: BulkReviewIn, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Approve all shown: every id is access-checked; foreign, unknown and malformed ids are skipped."""
    wanted = set(body.ids)
    ids = [PydanticObjectId(i) for i in wanted if PydanticObjectId.is_valid(i)]
    bids = await Bid.find({"_id": {"$in": ids}}).to_list()
    bidders = {b.id: b for b in await Bidder.find({"_id": {"$in": list({b.bidderId for b in bids})}}).to_list()}
    allowed = [b for b in bids if _can_review(bidders.get(b.bidderId), user)]
    if allowed:
        await Bid.get_motor_collection().update_many(
            {"_id": {"$in": [b.id for b in allowed]}}, {"$set": _decision(user, body.status, None)}
        )
        # One recount per bidder and report day, after all the writes (same race as the single review).
        sched = schedule_from_settings()
        per_day: dict[tuple, Bid] = {}
        for b in allowed:
            per_day.setdefault((b.bidderId, _next_daily_cutoff(b.firstAt.replace(tzinfo=timezone.utc), sched)), b)
        for b in per_day.values():
            await refresh_report_rows(b)
    return {"updated": len(allowed), "skipped": len(wanted) - len(allowed)}
