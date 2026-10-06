"""Bid records: each signed-upload request attaches its screenshot to the bid of that job folder."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from beanie import PydanticObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.models.bid import Bid
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
