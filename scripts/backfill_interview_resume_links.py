"""One-off backfill: link existing interviews to the resume they came from (Interview.resumeLink).

For each interview with no link: the same profile's finished generated resumes and uploaded-resume applications
whose company equals the interview's once normalised (case, punctuation, Inc/LLC/... ignored; "Acme" never links to
"Acme Health"), dated before its first round. The most recent wins and is linked with linkedBy="auto". Interviews
with no such match stay unlinked (the reason is unknown); people can link them from Edit details.

Dry run by default: prints what it would link. Idempotent: linked interviews are skipped.

Run:
    cd engineers_backend
    .venv/Scripts/python -m scripts.backfill_interview_resume_links            # dry run
    .venv/Scripts/python -m scripts.backfill_interview_resume_links --apply    # write
    add --score to score never-scored Generator resumes (one cached LLM extraction per distinct JD)

Spec: docs/superpowers/specs/2026-10-23-interview-resume-link-design.md
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections import Counter
from typing import Optional

from fastapi import HTTPException

from app.interview_resume_link import build_link, exact_matches

log = logging.getLogger(__name__)
SAMPLE = 15


def _first_round(doc: dict):
    dates = [r.get("scheduledAt") for r in doc.get("stageHistory") or [] if r.get("scheduledAt")]
    return min(dates) if dates else doc.get("scheduledAt") or doc.get("createdAt")


async def backfill(*, apply: bool = False, score: bool = False, limit: Optional[int] = None) -> dict:
    from app.models import Interview, UserExtra

    coll = Interview.get_motor_collection()
    cursor = coll.find(
        {"resumeLink": None},
        {"accountId": 1, "companyName": 1, "createdBy": 1, "scheduledAt": 1, "createdAt": 1, "stageHistory.scheduledAt": 1},
    )
    counts: Counter = Counter()
    samples: list[str] = []
    users: dict = {}
    async for doc in cursor:
        if limit is not None and counts["seen"] >= limit:
            break
        counts["seen"] += 1
        company, account = (doc.get("companyName") or "").strip(), doc.get("accountId")
        ids = await exact_matches(account, company, _first_round(doc)) if company and account else []
        if not ids:
            counts["unmatched"] += 1
            continue
        owner = doc.get("createdBy")
        if score and owner not in users:
            users[owner] = await UserExtra.get(owner)
        try:
            link = await build_link(ids[0], account, users.get(owner), linked_by="auto", score_missing=score)
        except HTTPException as exc:
            counts["errors"] += 1
            log.warning("interview %s: %s", doc["_id"], exc.detail)
            continue
        counts[link.kind] += 1
        counts["withJd"] += bool(link.jobDescription)
        counts["withScore"] += link.score is not None
        if len(samples) < SAMPLE:
            samples.append(
                f"{company!r} -> {link.kind} {link.matchedCompany!r} {link.appliedAt:%Y-%m-%d} "
                f"score={link.score.total if link.score else '-'} jd={'yes' if link.jobDescription else 'no'}"
            )
        if apply:
            res = await coll.update_one({"_id": doc["_id"], "resumeLink": None}, {"$set": {"resumeLink": link.model_dump()}})
            counts["written"] += res.modified_count
    return {"counts": dict(counts), "samples": samples}


async def _main() -> None:
    from app.config import get_settings
    from app.db import close_db, init_db

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="write the links (default: dry run)")
    parser.add_argument("--score", action="store_true", help="score never-scored Generator resumes (LLM calls)")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many interviews")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["MONGODB_URI"] = get_settings().MONGODB_URI
    await init_db()
    try:
        out = await backfill(apply=args.apply, score=args.score, limit=args.limit)
    finally:
        await close_db()
    print(("APPLIED" if args.apply else "DRY RUN") + ":", out["counts"])
    for line in out["samples"]:
        print("  ", line)


if __name__ == "__main__":
    asyncio.run(_main())
