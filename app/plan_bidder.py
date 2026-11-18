"""Bidder bids in the Daily Plan follow Bid Track: approved + pending bids of the owner's bidders, per report day."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from app.bids.windows import schedule_from_settings
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.profile_metrics import bid_report_day

log = logging.getLogger(__name__)

RELEASE_DAY = date(2026, 11, 18)  # days before this keep their typed numbers


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


async def bidder_done(owner_id, day: date) -> int:
    ids = [b.id for b in await Bidder.find({"createdBy": owner_id}).to_list()]
    if not ids:
        return 0
    sched = schedule_from_settings()
    n = 0
    async for d in Bid.get_motor_collection().find(
        {"bidderId": {"$in": ids}, "kind": {"$ne": "self"}, "status": {"$in": ["approved", "pending"]},
         "firstAt": {"$gte": _midnight(day) - timedelta(days=1), "$lt": _midnight(day) + timedelta(days=2)}},
        {"firstAt": 1},
    ):
        n += bid_report_day(d["firstAt"], sched) == day
    return n


async def write_through_bidder(owner_id, day: date) -> None:
    """Set the day's done.bidsBidder (plan created if missing, loggedAt untouched); re-render a submitted, posted plan."""
    try:
        if day < RELEASE_DAY:
            return
        from pymongo.errors import DuplicateKeyError

        from app.alerts import app_week_key

        n = await bidder_done(owner_id, day)
        coll = DailyPlan.get_motor_collection()
        key = {"userId": owner_id, "date": _midnight(day)}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        year, week_number, _key = app_week_key(_midnight(day))
        update = {"$set": {"done.bidsBidder": n, "updatedAt": now},
                  "$setOnInsert": {"year": year, "weekNumber": week_number, "createdAt": now}}
        try:
            await coll.update_one(key, update, upsert=True)
        except DuplicateKeyError:  # a concurrent first write created the plan between the upsert's find and insert
            await coll.update_one(key, update)
        from app.models import UserExtra
        from app.self_bids import resync_slack

        owner = await UserExtra.get(owner_id)
        if owner is not None:
            await resync_slack(owner, day, need_submitted=True)
    except Exception:
        log.exception("plan bidder: writing %s's bidder bids for %s failed", owner_id, day)
