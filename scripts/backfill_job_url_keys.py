"""One-off backfill: ResumeApplication.jobUrlKey (the normalised job link) for resumes made before it was set on
insert, so Statistics → Resume compare finds every profile's resume for the same job.

--rekey also rewrites keys made under older URL rules (app/job_apply/urls.py URL_VERSION): run it once after a bump.

Dry run by default: counts the resumes it would fill. Idempotent: resumes whose key is already right are skipped.

Run:
    cd engineers_backend
    .venv/Scripts/python -m scripts.backfill_job_url_keys                    # dry run
    .venv/Scripts/python -m scripts.backfill_job_url_keys --apply            # write missing keys
    .venv/Scripts/python -m scripts.backfill_job_url_keys --rekey --apply    # also fix stale keys
"""
from __future__ import annotations

import argparse
import asyncio
import os

from app.models.resume_application import job_url_key

CHUNK = 200  # updates sent at once


async def backfill(*, apply: bool = False, rekey: bool = False) -> dict:
    from app.models import ResumeApplication

    coll = ResumeApplication.get_motor_collection()
    query = {"jobUrl": {"$nin": [None, ""]}} if rekey else {"jobUrlKey": None, "jobUrl": {"$nin": [None, ""]}}
    todo: list[tuple] = []
    async for d in coll.find(query, {"jobUrl": 1, "jobUrlKey": 1}):
        key = job_url_key(d["jobUrl"])
        if key and key != d.get("jobUrlKey"):
            todo.append((d["_id"], d.get("jobUrlKey"), key))
    written = 0
    if apply:
        for i in range(0, len(todo), CHUNK):
            results = await asyncio.gather(*(
                coll.update_one({"_id": _id, "jobUrlKey": old}, {"$set": {"jobUrlKey": key}})
                for _id, old, key in todo[i:i + CHUNK]
            ))
            written += sum(r.modified_count for r in results)
    return {"resumes": len(todo), "written": written}


async def _main() -> None:
    from app.config import get_settings
    from app.db import close_db, init_db

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="write the keys (default: dry run)")
    parser.add_argument("--rekey", action="store_true", help="also rewrite keys made under older URL rules")
    args = parser.parse_args()
    os.environ["MONGODB_URI"] = get_settings().MONGODB_URI
    await init_db()
    try:
        out = await backfill(apply=args.apply, rekey=args.rekey)
    finally:
        await close_db()
    print(("APPLIED" if args.apply else "DRY RUN") + f": {out['resumes']} resumes to key, {out['written']} written")


if __name__ == "__main__":
    asyncio.run(_main())
