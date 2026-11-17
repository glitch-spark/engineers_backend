"""One-off: assigning a caller now confirms the round. Rounds assigned before that change may still read
confirmed=False; this sets confirmed=True on them.

A round is picked when its caller request is on, a person is assigned (a platform user or an external caller, not
"TBD"), it is not canceled or rejected, `confirmed` is not True, and its scheduledAt is in the future or within the
last --days days (default 7). The interview-level `confirmed` mirror is set too when the round is the latest one.
Only those `confirmed` fields are written, with positional ($set stageHistory.<i>.confirmed) updates guarded by the
round's id; nothing else changes.

Dry run by default: prints the rounds grouped by interview. Idempotent.

Run:
    cd engineers_backend
    .venv/Scripts/python -m scripts.backfill_round_confirmed            # dry run
    .venv/Scripts/python -m scripts.backfill_round_confirmed --apply    # write

Connects with MONGODB_URI and registers the models only: no startup migrations or seeds, no index creation.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from app.interview_caller import ENDED_STATUSES, caller_unassigned
from app.interview_normalize import normalize_stage, normalize_status


@dataclass
class Target:
    interview_id: Any
    company: str
    # (index in stageHistory, round id or None, stage, scheduledAt, caller name, is the latest round)
    rounds: list = field(default_factory=list)


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _needs_confirm(rnd: dict, cutoff: datetime) -> bool:
    caller = rnd.get("caller") or {}
    if not caller.get("enabled") or caller_unassigned(SimpleNamespace(**caller)):
        return False
    if rnd.get("confirmed") is True or normalize_status(rnd.get("status")) in ENDED_STATUSES:
        return False
    at = _naive_utc(rnd.get("scheduledAt"))
    return at is not None and at >= cutoff


async def plan_backfill(now: datetime, days: int = 7) -> list[Target]:
    from app.models import Interview

    cutoff = _naive_utc(now) - timedelta(days=days)
    targets: list[Target] = []
    cursor = Interview.get_motor_collection().find(
        {"stageHistory": {"$elemMatch": {"caller.enabled": True, "confirmed": {"$ne": True}}}},
        {"companyName": 1, "stageHistory": 1},
    )
    async for doc in cursor:
        history = doc.get("stageHistory") or []
        target = Target(doc["_id"], (doc.get("companyName") or "").strip() or f"(no company, {doc['_id']})")
        for i, rnd in enumerate(history):
            if _needs_confirm(rnd, cutoff):
                target.rounds.append((
                    i, rnd.get("id"), normalize_stage(rnd.get("stage")) or "?", _naive_utc(rnd.get("scheduledAt")),
                    (rnd.get("caller") or {}).get("callerName") or "?", i == len(history) - 1,
                ))
        if target.rounds:
            targets.append(target)
    targets.sort(key=lambda t: min(r[3] for r in t.rounds))
    return targets


async def apply_backfill(targets: list[Target]) -> int:
    """Set confirmed=True on the planned rounds. A round that moved or was confirmed since the scan is skipped."""
    from app.models import Interview

    coll = Interview.get_motor_collection()
    written = 0
    for t in targets:
        for i, round_id, *_rest, latest in t.rounds:
            guard: dict = {"_id": t.interview_id, f"stageHistory.{i}.confirmed": {"$ne": True}}
            if round_id:
                guard[f"stageHistory.{i}.id"] = round_id
            sets: dict = {f"stageHistory.{i}.confirmed": True}
            if latest:
                sets["confirmed"] = True  # the interview mirrors its latest round
            res = await coll.update_one(guard, {"$set": sets})
            written += res.modified_count
    return written


def print_plan(targets: list[Target]) -> None:
    total = sum(len(t.rounds) for t in targets)
    print(f"Rounds to confirm: {total} in {len(targets)} interview(s)")
    for t in targets:
        print(f"  {t.company} ({t.interview_id})")
        for _i, _rid, stage, at, name, _latest in t.rounds:
            print(f"     - {stage} {at:%Y-%m-%d %H:%M} UTC · caller {name}")


async def run(args: argparse.Namespace, now: Optional[datetime] = None) -> int:
    targets = await plan_backfill(now or datetime.now(timezone.utc), days=args.days)
    print_plan(targets)
    if args.apply:
        written = await apply_backfill(targets)
        print(f"APPLIED: confirmed {written} round(s)")
    else:
        print("DRY RUN: nothing written. Re-run with --apply to confirm them.")
    return 0


async def _connect():
    """Mongo + the models this script reads and writes. Deliberately not app.db.init_db (it writes migrations/seeds)."""
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.config import get_settings
    from app.models import Account, Caller, Interview, InterviewQuestion, UserExtra

    client = AsyncIOMotorClient(get_settings().MONGODB_URI)
    await init_beanie(
        database=client.get_default_database(),
        document_models=[Interview, UserExtra, Account, InterviewQuestion, Caller],
        skip_indexes=True,
    )
    return client


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="write confirmed=True (default: dry run)")
    parser.add_argument("--days", type=int, default=7, help="also include rounds that started this many days ago")
    args = parser.parse_args()
    client = await _connect()
    try:
        return await run(args)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
