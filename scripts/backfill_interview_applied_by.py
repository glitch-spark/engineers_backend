"""Backfill "Applied by" on interviews saved since Bid Track started (Oct 6, 2026) that have none.

An interview counts as bidder-applied when, at the moment it was saved, its profile had a real bidder:
- active (not archived yet),
- with at least MIN_BIDS approved + pending bids overall (test bidders made a handful),
- that had already bid (first bid on or before the save).
It goes to the one with the most bids; otherwise it's self-applied. Interviews saved before Oct 6, and any that
already have "Applied by", are left alone. Every written entry carries backfilled=True.

    .venv/Scripts/python -m scripts.backfill_interview_applied_by                         # dry run: counts only
    .venv/Scripts/python -m scripts.backfill_interview_applied_by --apply --backup FILE   # write (FILE: what changed)

Connects without app.db.init_db (no migrations, no index builds): the only writes are the backfill's own.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId

from app.models import Bid, Bidder, Interview

SINCE = datetime(2026, 10, 6, tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc).replace(tzinfo=None)
MIN_BIDS = 50


async def plan() -> list[tuple[PydanticObjectId, dict]]:
    """(interview id, appliedBy to write) for every interview the backfill would set."""
    by_profile: dict = collections.defaultdict(list)
    async for b in Bidder.get_motor_collection().find({"profileId": {"$ne": None}}, {"name": 1, "profileId": 1, "archivedAt": 1}):
        by_profile[b["profileId"]].append(b)
    stats: dict = {}
    pipeline = [{"$match": {"status": {"$in": ["approved", "pending"]}}},
                {"$group": {"_id": "$bidderId", "n": {"$sum": 1}, "first": {"$min": "$firstAt"}}}]
    for g in await Bid.get_motor_collection().aggregate(pipeline).to_list(None):
        stats[g["_id"]] = (g["n"], g["first"])
    out = []
    cursor = Interview.get_motor_collection().find({"appliedBy": None, "createdAt": {"$gte": SINCE}}, {"accountId": 1, "createdAt": 1})
    async for iv in cursor:
        saved = iv["createdAt"]
        real = []
        for b in by_profile.get(iv.get("accountId"), []):
            n, first = stats.get(b["_id"], (0, None))
            archived = b.get("archivedAt")
            if n >= MIN_BIDS and first is not None and first <= saved and (archived is None or archived >= saved):
                real.append((n, b))
        if real:
            _n, top = max(real, key=lambda nb: nb[0])
            out.append((iv["_id"], {"kind": "bidder", "bidderId": top["_id"], "bidderName": top["name"], "backfilled": True}))
        else:
            out.append((iv["_id"], {"kind": "self", "backfilled": True}))
    return out


async def apply(planned: list[tuple[PydanticObjectId, dict]]) -> int:
    """Write each planned entry only if the interview still has no "Applied by"; returns how many were written."""
    written = 0
    coll = Interview.get_motor_collection()
    for iv_id, applied in planned:
        res = await coll.update_one({"_id": iv_id, "appliedBy": None}, {"$set": {"appliedBy": applied}})
        written += res.modified_count
    return written


async def main() -> None:
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.config import get_settings
    from app.db import DOCUMENT_MODELS

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--backup", help="with --apply: JSON file listing every interview id and what was written")
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("--apply needs --backup FILE")
    client = AsyncIOMotorClient(get_settings().MONGODB_URI)
    await init_beanie(database=client.get_default_database(), document_models=DOCUMENT_MODELS, skip_indexes=True)
    planned = await plan()
    kinds = collections.Counter(a["kind"] for _i, a in planned)
    bidders = collections.Counter(a["bidderName"] for _i, a in planned if a["kind"] == "bidder")
    print(f"interviews saved since {SINCE:%Y-%m-%d} UTC with no Applied by: {len(planned)} -> {dict(kinds)}")
    print("bidder-applied by bidder:", dict(bidders.most_common()))
    if not args.apply:
        print("dry run: nothing written (add --apply --backup FILE to write)")
        return
    with open(args.backup, "w", encoding="utf8") as f:
        json.dump([{"interviewId": str(i), "appliedBy": {**a, **({"bidderId": str(a["bidderId"])} if "bidderId" in a else {})}}
                   for i, a in planned], f, indent=1)
    print(f"backup written: {args.backup} ({len(planned)} entries); to undo: $unset appliedBy where appliedBy.backfilled")
    print("written:", await apply(planned))


if __name__ == "__main__":
    asyncio.run(main())
