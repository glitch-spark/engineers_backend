"""One-off backfill: fixed-resume forks for the Job Applies runs still alive (runs are deleted 7 days after upload).

Every scored row of every live run is synced the way scoring now does it (app/job_apply/fixed_forks.py): the best
uploaded resume at or above the run's minimum, for a profile the job is open to, becomes the job's own copy. Rows with a
tailored resume are left alone. Run it before scripts/backfill_interview_resume_links.py so interviews can link to
the forks.

Dry run by default: counts the forks it would create. Idempotent: a row whose fork is current is skipped.

Run:
    cd engineers_backend
    .venv/Scripts/python -m scripts.backfill_fixed_forks            # dry run
    .venv/Scripts/python -m scripts.backfill_fixed_forks --apply    # write

Spec: docs/superpowers/specs/2026-10-24-fixed-resume-forks-design.md
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from app.job_apply import fixed_forks


async def backfill(*, apply: bool = False) -> dict:
    from app.models.job_apply import JobApplyRow, JobApplyRun

    runs = await JobApplyRun.find({}).to_list()
    total = 0
    per_run: dict[str, int] = {}
    for run in runs:
        rows = await JobApplyRow.find({"runId": run.id, "scoredAt": {"$ne": None}}).to_list()
        if not rows:
            continue
        n = await fixed_forks.sync_rows(run, rows, dry_run=not apply)
        if n:
            per_run[f"{run.fileName} ({run.id})"] = n
        total += n
    return {"runs": len(runs), "forks": total, "perRun": per_run}


async def _main() -> None:
    from app.config import get_settings
    from app.db import close_db, init_db

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="create the forks (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["MONGODB_URI"] = get_settings().MONGODB_URI
    await init_db()
    try:
        out = await backfill(apply=args.apply)
    finally:
        await close_db()
    print(("APPLIED" if args.apply else "DRY RUN") + f": {out['forks']} forks across {out['runs']} live runs")
    for name, n in out["perRun"].items():
        print(f"   {n:4d}  {name}")


if __name__ == "__main__":
    asyncio.run(_main())
