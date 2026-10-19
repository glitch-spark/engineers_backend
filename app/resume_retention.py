"""Retention for generated resumes. After RESUME_TEXT_RETENTION_DAYS a completed job keeps its metadata
and PDF but drops the heavy text (job description, resume text, cover letter, screening answers), which
is ~90% of the collection. The dropped text is archived to the resume S3 bucket first (gzipped JSON
lines, one object per batch) so it can be restored; without S3 nothing is stripped.

Run daily from the cron (app/alerts_tick.py) or by hand: python -m app.resume_retention
"""
from __future__ import annotations

import asyncio
import gzip
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import json_util

from app.config import get_settings
from app.models import ResumeApplication
from app.resume_service import _s3_client, s3_enabled

log = logging.getLogger(__name__)

# jobDescription is a required str on the model, so it's blanked instead of unset.
HEAVY_FIELDS = (
    "jobDescription", "tailorJobDescription", "plainText", "coverLetterText", "screeningPairs", "resumeLlmFallbackReason",
)
ARCHIVE_PREFIX = "archive/resume-text"
BATCH_SIZE = 500
# The cron ticks every 5 minutes; only the tick in this UTC window runs the daily pass.
DAILY_HOUR_UTC = 9
DAILY_WINDOW_MINUTES = 5


def _stale_filter(cutoff: datetime) -> dict:
    has_text = [
        {"jobDescription": {"$nin": [None, ""]}},
        {"tailorJobDescription": {"$nin": [None, ""]}},
        {"plainText": {"$nin": [None, ""]}},
        {"coverLetterText": {"$nin": [None, ""]}},
        {"screeningPairs.0": {"$exists": True}},
        {"resumeLlmFallbackReason": {"$nin": [None, ""]}},
    ]
    return {"status": "completed", "createdAt": {"$lt": cutoff}, "$or": has_text}


def _archive_key(now: datetime, first_id: object) -> str:
    return f"{ARCHIVE_PREFIX}/{now:%Y/%m/%d}/{now:%H%M%S}-{first_id}.jsonl.gz"


async def _archive(key: str, docs: list[dict]) -> None:
    body = gzip.compress("\n".join(json_util.dumps(d) for d in docs).encode())
    bucket = get_settings().AWS_S3_BUCKET
    await asyncio.to_thread(
        _s3_client().put_object,
        Bucket=bucket, Key=key, Body=body, ContentType="application/gzip", ServerSideEncryption="AES256",
    )


async def strip_old_resume_text(
    *, days: Optional[int] = None, now: Optional[datetime] = None, max_batches: int = 100,
) -> dict:
    """Archive then strip the heavy text of completed jobs older than `days`. Returns counts."""
    days = get_settings().RESUME_TEXT_RETENTION_DAYS if days is None else days
    if days <= 0:
        return {"skipped": "off"}
    if not s3_enabled():
        return {"skipped": "no-s3"}  # never strip what can't be archived
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    coll = ResumeApplication.get_motor_collection()
    projection = {f: 1 for f in HEAVY_FIELDS}
    stripped = batches = 0
    while batches < max_batches:
        docs = await coll.find(_stale_filter(cutoff), projection).sort("createdAt", 1).to_list(BATCH_SIZE)
        if not docs:
            break
        key = _archive_key(now, docs[0]["_id"])
        await _archive(key, docs)
        res = await coll.update_many(
            {"_id": {"$in": [d["_id"] for d in docs]}},
            {
                "$set": {"jobDescription": "", "textArchivedAt": now, "textArchiveKey": key},
                "$unset": {f: "" for f in HEAVY_FIELDS if f != "jobDescription"},
            },
        )
        stripped += res.modified_count
        batches += 1
        log.info("resume retention: archived %d jobs to %s", len(docs), key)
    return {"stripped": stripped, "batches": batches, "cutoffDays": days}


def daily_due(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now.hour == DAILY_HOUR_UTC and now.minute < DAILY_WINDOW_MINUTES


async def run_retention_tick(now: Optional[datetime] = None) -> dict:
    """Cron entry: the daily pass, or not-due."""
    if not daily_due(now):
        return {"skipped": "not-due"}
    return await strip_old_resume_text(now=now)


async def _main() -> None:
    import os

    from app.db import close_db, init_db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["MONGODB_URI"] = get_settings().MONGODB_URI
    await init_db()
    try:
        log.info("resume retention: %s", await strip_old_resume_text())
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(_main())
