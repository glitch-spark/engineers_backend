"""Fit score for interviews without a scored resume (one-off; never run by the cron).

    python -m scripts.score_interview_fit [--since 2026-10-06]           # dry run: candidate counts only, no LLM
    python -m scripts.score_interview_fit [--since 2026-10-06] --apply   # score and write

Candidates: interviews (created or scheduled since `--since`) linked as fixed / uploaded / none, with a job description
on the link and no `fitScore` yet. The resume scored: a fixed link's fork text (resumeapplications.plainText, or its
retention archive); a link that already carries a score (resumeLink.score) is copied as is (`copied`, no LLM); an uploaded link's profile resume (Account.resumes[id == resumeLink.resumeId].markdown); else the
profile's latest uploaded resume (Account.resumes, max uploadedAt). Scored like a Resume Generator resume
(`tailored_score.score_manual`, for the profile's owner) and stored as `fitScore: {total, band}` with `updatedAt` bumped; the applications
sync gives it to the matched row when that row has no tailored score. Idempotent: scored interviews are skipped, an
unscorable one is retried next run. Connects without app.db.init_db, like scripts/build_applications.py.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import Counter
from datetime import date, datetime

from app.job_apply import tailored_score
from app.job_apply.scoring import band
from app.models import Account, Interview, ResumeApplication
from app.models.user_extra import UserExtra
from app.resume_retention import archived_text
from app.stats.conversion_scope import DATA_START

log = logging.getLogger(__name__)

KINDS = ("fixed", "uploaded", "none")
CONCURRENCY = 3  # scoring calls at once (each may run an LLM job extraction)
CALL_TIMEOUT = 120.0  # seconds per scoring call


async def _resume_text(link: dict, account: dict) -> str:
    if link.get("kind") == "fixed" and link.get("resumeJobId"):
        d = await ResumeApplication.get_motor_collection().find_one(
            {"_id": link["resumeJobId"]}, {"plainText": 1, "textArchiveKey": 1})
        if d:
            text = d.get("plainText") or ((await archived_text(d.get("textArchiveKey"), d["_id"])) or {}).get("plainText")
            if (text or "").strip():
                return text
    resumes = account.get("resumes") or []
    picked = next((r for r in resumes if link.get("resumeId") and str(r.get("id")) == link["resumeId"]), None)
    picked = picked or max(resumes, key=lambda r: r.get("uploadedAt") or datetime.min, default={})
    return picked.get("markdown") or ""


async def score_interview_fit(apply: bool, since: date = DATA_START) -> dict:
    since_dt = datetime(since.year, since.month, since.day)
    coll = Interview.get_motor_collection()
    cands = [d async for d in coll.find({
        "fitScore": {"$exists": False}, "resumeLink.kind": {"$in": list(KINDS)},
        "resumeLink.jobDescription": {"$nin": [None, ""]},
        "$or": [{"createdAt": {"$gte": since_dt}}, {"scheduledAt": {"$gte": since_dt}}],
    }, {"accountId": 1, "resumeLink": 1}) if d["resumeLink"]["jobDescription"].strip()]
    counts = {"candidates": len(cands), "byKind": dict(Counter(d["resumeLink"]["kind"] for d in cands)),
              "scored": 0, "copied": 0, "noResume": 0, "unscorable": 0}
    accounts = {a["_id"]: a async for a in Account.get_motor_collection().find(
        {"_id": {"$in": list({d["accountId"] for d in cands})}}, {"resumes": 1, "createdBy": 1})}
    owners = {o.id: o for o in await UserExtra.find(
        {"_id": {"$in": list({a.get("createdBy") for a in accounts.values()} - {None})}}).to_list()}
    sem = asyncio.Semaphore(CONCURRENCY)

    async def write(d: dict, total, band) -> None:
        # updatedAt bumped so the incremental applications sync picks the interview up
        await coll.update_one({"_id": d["_id"], "fitScore": {"$exists": False}},
                              {"$set": {"fitScore": {"total": total, "band": band}, "updatedAt": datetime.utcnow()}})

    async def one(d: dict) -> None:
        link, account = d["resumeLink"], accounts.get(d["accountId"]) or {}
        linked = link.get("score") or {}  # a TailoredMatch dict already scored when linked: no LLM call needed
        if linked.get("total") is not None:
            if apply:
                await write(d, linked["total"], linked.get("band") or band(linked["total"]))
            counts["copied"] += 1
            return
        async with sem:
            text = await _resume_text(link, account)
            if not text.strip():
                counts["noResume"] += 1
                return
            if not apply:
                return
            try:
                m = await asyncio.wait_for(tailored_score.score_manual(
                    link["jobDescription"], text, owners.get(account.get("createdBy"))), CALL_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 — counted and logged; retried on the next run
                # type only: the message can carry resume / job description text
                log.warning("fit score for interview %s failed: %s", d["_id"], type(exc).__name__)
                m = None
        if m is None:
            counts["unscorable"] += 1
            return
        await write(d, m.total, m.band)
        counts["scored"] += 1

    async def safe(d: dict) -> None:
        try:
            await one(d)
        except Exception as exc:  # noqa: BLE001 — a failed read or write is counted; the batch goes on
            log.warning("fit score for interview %s failed: %s", d["_id"], type(exc).__name__)
            counts["unscorable"] += 1

    await asyncio.gather(*(safe(d) for d in cands))
    return counts


async def main() -> None:
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.config import get_settings
    from app.db import DOCUMENT_MODELS

    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--since", type=date.fromisoformat, default=DATA_START, help="YYYY-MM-DD (default 2026-10-06)")
    parser.add_argument("--apply", action="store_true", help="score and write (default: dry run, no LLM calls)")
    args = parser.parse_args()
    client = AsyncIOMotorClient(get_settings().MONGODB_URI)
    await init_beanie(database=client.get_default_database(), document_models=DOCUMENT_MODELS, skip_indexes=True)
    t0 = time.monotonic()
    counts = await score_interview_fit(args.apply, args.since)
    print(f"interview fit scores since {args.since}: {counts} in {time.monotonic() - t0:.0f}s")
    print("written" if args.apply else "dry run: nothing scored or written (add --apply)")


if __name__ == "__main__":
    asyncio.run(main())
