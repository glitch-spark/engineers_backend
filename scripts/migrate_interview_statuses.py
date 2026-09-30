"""Turn the legacy "rejected" stage into round statuses and map legacy values.

Run once after deploying the five-status model:
  python -m scripts.migrate_interview_statuses          # dry run
  python -m scripts.migrate_interview_statuses --apply

Idempotent. Rules live in app.interview_normalize.resolve_interview:
- a trailing "rejected" round is removed; the round before it becomes
  canceled (old status scheduled) or rejected, inheriting its note/transcript
  and extracted questions,
- a rejected-only history becomes an Intro round (ids listed below),
- earlier rounds with no status become passed,
- legacy values (failed, no_show, rescheduled, tech, tech1, …) are mapped,
- interview-level stage/status/scheduledAt are re-synced from the last round,
- extracted questions follow their round: stageId re-pointed off removed
  rounds, and their `stage` snapshot set to that round's stage (the Analyze
  tab groups by it, so no "rejected" group survives).

Works on the raw collections: the Interview model applies the same rules on
read, so going through Beanie would hide every difference.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.interview_normalize import Resolution, question_updates, resolve_interview  # noqa: E402

_FIELDS = ("stage", "status", "scheduledAt", "endsAt", "stageHistory")


def changed_fields(raw: dict, doc: dict) -> dict:
    """The $set payload: top-level migrated fields whose value changed."""
    return {k: doc.get(k) for k in _FIELDS if doc.get(k) != raw.get(k)}


def plan_interview(raw: dict, questions: list[dict], *, now: datetime) -> tuple[dict, list[tuple], Resolution]:
    """For one raw interview and its questions: the interview $set payload,
    the question updates, and the resolution (rule counts, remap)."""
    res = resolve_interview(raw, now=now)
    iv_id = raw["_id"]
    remap = {(iv_id, old): new for old, new in res.question_remap.items()}
    stage_by_round = {
        (iv_id, e["id"]): e.get("stage") for e in res.doc.get("stageHistory") or [] if e.get("id")
    }
    return changed_fields(raw, res.doc), question_updates(questions, remap, stage_by_round), res


async def run(apply: bool) -> int:
    db = AsyncIOMotorClient(get_settings().MONGODB_URI).get_default_database()
    now = datetime.now(timezone.utc)
    scanned = changed = remapped_rounds = 0
    rules: Counter = Counter()
    intro_ids: list = []
    all_q_updates: list[tuple] = []

    questions = await db.interviewquestions.find(
        {}, {"interviewId": 1, "stageId": 1, "stage": 1},
    ).to_list(None)
    by_interview: dict = defaultdict(list)
    for q in questions:
        by_interview[q.get("interviewId")].append(q)

    async for raw in db.interviews.find({}):
        scanned += 1
        payload, q_updates, res = plan_interview(raw, by_interview.pop(raw["_id"], []), now=now)
        rules.update(res.rules)
        remapped_rounds += len(res.question_remap)
        if res.rules.get("rejected_only_to_intro"):
            intro_ids.append(raw["_id"])
        all_q_updates += q_updates
        if payload:
            changed += 1
        if apply:
            # Questions first: once the interview is rewritten a re-run can no
            # longer see the removed round, so its questions must already follow.
            for qid, q_payload in q_updates:
                await db.interviewquestions.update_one({"_id": qid}, {"$set": q_payload})
            if payload:
                await db.interviews.update_one({"_id": raw["_id"]}, {"$set": payload})

    # Questions of deleted interviews: only map legacy stage values.
    orphans = [q for qs in by_interview.values() for q in qs]
    orphan_updates = question_updates(orphans, {}, {})
    all_q_updates += orphan_updates
    if apply:
        for qid, q_payload in orphan_updates:
            await db.interviewquestions.update_one({"_id": qid}, {"$set": q_payload})

    relinked = sum(1 for _, q in all_q_updates if "stageId" in q)
    new_stage = {qid: p["stage"] for qid, p in all_q_updates if "stage" in p}
    stage_before = {q.get("stage") for q in questions} - {None}
    stage_after = {new_stage.get(q["_id"], q.get("stage")) for q in questions} - {None}

    mode = "applied" if apply else "dry-run"
    print(f"mode={mode} scanned={scanned} changed={changed}")
    for name in sorted(rules):
        print(f"  {name}: {rules[name]}")
    print(f"  removed rounds with questions re-pointed: {remapped_rounds}")
    print(f"  questions updated: {len(all_q_updates)} (re-linked to previous round: {relinked})")
    print(f"  question stage values before: {sorted(stage_before)}")
    print(f"  question stage values after:  {sorted(stage_after)}")
    print(f"  rejected-only -> intro ids ({len(intro_ids)}): {', '.join(str(i) for i in intro_ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(apply="--apply" in sys.argv)))
