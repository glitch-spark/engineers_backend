"""Build the `applications` collection (bids + tailored resumes + matched interviews) from a date on.

    python -m scripts.build_applications [--since 2026-08-01]           # dry run: counts only
    python -m scripts.build_applications [--since 2026-08-01] --apply   # write

Idempotent (rows are upserted on their key); rows whose source is gone are deleted. --apply also records the sync
state like the cron's full rebuild (checkpoint = lastFull = run start), so the cron continues incrementally. Connects without
app.db.init_db (no migrations, no index builds): the only writes are the rows themselves.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from datetime import date, datetime
from typing import Optional

from app.applications.sync import BACKFILL_SINCE, full_rebuild, save_state


async def build(since: date, *, apply: bool, start: Optional[datetime] = None) -> dict:
    """Full rebuild from `since`; on apply, seed the sync state with the run start (captured before any read)."""
    start = start or datetime.utcnow()
    counts = await full_rebuild(since, apply=apply)
    if apply:
        await save_state({"checkpoint": start, "lastFull": start})
    return counts


async def main() -> None:
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.config import get_settings
    from app.db import DOCUMENT_MODELS

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--since", type=date.fromisoformat, default=BACKFILL_SINCE, help="YYYY-MM-DD (default 2026-08-01)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()
    import app.applications.sync as sync

    sync.SR_BUDGET = None  # a manual rebuild looks up every pending SmartRecruiters bid, however long it takes
    client = AsyncIOMotorClient(get_settings().MONGODB_URI)
    await init_beanie(database=client.get_default_database(), document_models=DOCUMENT_MODELS, skip_indexes=True)
    t0 = time.monotonic()
    counts = await build(args.since, apply=args.apply)
    print(f"applications since {args.since}: {counts} in {time.monotonic() - t0:.0f}s")
    print("written" if args.apply else "dry run: nothing written (add --apply to write)")


if __name__ == "__main__":
    asyncio.run(main())
