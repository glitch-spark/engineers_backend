"""Bid records: each signed-upload request attaches its screenshot to the bid of that job folder."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional

from beanie import PydanticObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.bids.windows import BidSchedule
from app.config import Settings, get_settings
from app.models.bid import Bid
from app.models.bid_report import BidReport
from app.models.bid_report_post import BidReportPost
from app.models.bidder import Bidder


def job_key(key: str) -> str:
    """The job folder: a screenshot key without its file name."""
    return key.rsplit("/", 1)[0]


def _naive_utc(at: datetime) -> datetime:
    """Stored datetimes are naive UTC like the other models; a naive input is taken as UTC already."""
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc).replace(tzinfo=None)
    return at


async def record_screenshot(
    bidder: Bidder, key: str, step: int, trigger: str, captured_at: datetime,
    job_url: Optional[str], job_title: Optional[str],
) -> Bid:
    """Upsert the bid of this bidder and job folder and attach the screenshot (a retried key is not added twice)."""
    coll = Bid.get_motor_collection()
    jk, at, now = job_key(key), _naive_utc(captured_at), datetime.utcnow()

    upsert = (
        {"bidderId": bidder.id, "jobKey": jk},
        {
            "$setOnInsert": {
                "ownerId": bidder.createdBy, "jobKey": jk, "status": "pending", "createdAt": now,
                "screenshots": [], "changedSinceReview": False,
            },
            "$min": {"firstAt": at},
            "$set": {"updatedAt": now},
        },
    )
    try:
        doc = await coll.find_one_and_update(*upsert, upsert=True, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:  # a parallel first screenshot of this job inserted the bid between our check and insert
        doc = await coll.find_one_and_update(*upsert, upsert=True, return_document=ReturnDocument.AFTER)
    bid_id = doc["_id"]

    shot = {"key": key, "step": step, "trigger": trigger, "capturedAt": at, "uploadedAt": None}
    pushed = await coll.update_one(
        {"_id": bid_id, "screenshots.key": {"$ne": key}}, {"$push": {"screenshots": shot}}
    )
    if pushed.modified_count:
        # A decision stands; the reviewer is told that something was added after it.
        await coll.update_one({"_id": bid_id, "status": {"$ne": "pending"}}, {"$set": {"changedSinceReview": True}})
    if job_url:
        await coll.update_one({"_id": bid_id, "jobUrl": None}, {"$set": {"jobUrl": job_url}})
    if job_title:
        await coll.update_one({"_id": bid_id, "jobTitle": None}, {"$set": {"jobTitle": job_title}})
    if trigger == "submit":
        await coll.update_one({"_id": bid_id, "submittedAt": None}, {"$set": {"submittedAt": at}})

    return await Bid.get(bid_id)


async def confirm_upload(bidder_id: PydanticObjectId, key: str) -> bool:
    """Mark the screenshot with this key uploaded; False when it is not in one of this bidder's bids."""
    result = await Bid.get_motor_collection().update_one(
        {"bidderId": bidder_id, "screenshots.key": key},
        {"$set": {"screenshots.$.uploadedAt": datetime.utcnow()}},
    )
    return result.matched_count > 0


def records_start_utc(sched: BidSchedule, s: Optional[Settings] = None) -> Optional[datetime]:
    """Local midnight of BID_RECORDS_START in the report timezone, as an aware UTC datetime; None when unset."""
    start = (s or get_settings()).BID_RECORDS_START
    if not start:
        return None
    return datetime.combine(date.fromisoformat(start), time.min, tzinfo=sched.tz).astimezone(timezone.utc)


def uses_records(window_start: datetime, sched: BidSchedule, s: Optional[Settings] = None) -> bool:
    """A window counts bid records (the database), not screenshot folders. BID_RECORDS_START only keeps older
    windows on folder counts: one starting before local midnight of that day (or straddling it) counts folders."""
    start = records_start_utc(sched, s)
    return start is None or window_start >= start


async def count_by_status(
    bidder_ids: list[PydanticObjectId], start: datetime, end: datetime
) -> dict[str, tuple[int, int, int]]:
    """(approved, pending, rejected) bids of each bidder whose first screenshot is in [start, end).

    Every id is in the result, with zeros when it has no bids."""
    counts = {str(i): (0, 0, 0) for i in bidder_ids}
    if not counts:
        return counts
    pipeline = [
        {"$match": {"bidderId": {"$in": list(bidder_ids)}, "firstAt": {"$gte": _naive_utc(start), "$lt": _naive_utc(end)}}},
        {"$group": {"_id": {"bidderId": "$bidderId", "status": "$status"}, "n": {"$sum": 1}}},
    ]
    slot = {"approved": 0, "pending": 1, "rejected": 2}
    for g in await Bid.get_motor_collection().aggregate(pipeline).to_list(None):
        key, i = str(g["_id"]["bidderId"]), slot[g["_id"]["status"]]
        counts[key] = counts[key][:i] + (g["n"],) + counts[key][i + 1:]
    return counts


async def refresh_report_rows(bid: Bid) -> None:
    """Recount the stored daily and weekly report rows of this bid's bidder whose period holds the bid.

    Rows counted from folders (no `pending`) are left as they were. A row whose counts changed marks its
    period's Slack post dirty, so the next cron tick edits it in place."""
    at = _naive_utc(bid.firstAt)
    coll = BidReport.get_motor_collection()
    stale = await coll.find({
        "bidderId": bid.bidderId, "pending": {"$ne": None}, "periodStart": {"$lte": at}, "periodEnd": {"$gt": at},
    }).to_list(None)
    for row in stale:
        counts = await count_by_status([bid.bidderId], row["periodStart"], row["periodEnd"])
        approved, pending, rejected = counts[str(bid.bidderId)]
        if (row.get("count"), row.get("pending"), row.get("rejected")) == (approved, pending, rejected):
            continue
        await coll.update_one({"_id": row["_id"]}, {"$set": {
            "count": approved, "pending": pending, "rejected": rejected,
            "amount": round(approved * row["rate"], 2), "updatedAt": datetime.utcnow(),
        }})
        await BidReportPost.get_motor_collection().update_one(
            {"kind": row["kind"], "periodKey": row["periodKey"]}, {"$set": {"dirty": True}, "$inc": {"rev": 1}},
        )
