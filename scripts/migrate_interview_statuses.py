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
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.interview_normalize import normalize_stage, resolve_interview  # noqa: E402

_FIELDS = ("stage", "status", "scheduledAt", "stageHistory")


def changed_fields(raw: dict, doc: dict) -> dict:
    """The $set payload: top-level migrated fields whose value changed."""
    return {k: doc.get(k) for k in _FIELDS if doc.get(k) != raw.get(k)}


def question_updates(
    questions: list[dict],
    remap: dict[tuple, str],
    stage_by_round: dict[tuple, str],
) -> list[tuple]:
    """(question _id, $set payload) for questions whose round link or stage changes.

    `remap` and `stage_by_round` are keyed by (interviewId, roundId) so ids are
    never matched across interviews. Questions without a known round only get
    their legacy stage value mapped.
    """
    out = []
    for q in questions:
        iv_id, sid = q.get("interviewId"), q.get("stageId")
        new_sid = remap.get((iv_id, sid), sid)
        old_stage = q.get("stage")
        new_stage = old_stage
        # `stage` is an extraction-time snapshot: only rewrite it when the
        # question moved rounds or holds "rejected" / a legacy stage key.
        if new_sid != sid or old_stage == "rejected" or normalize_stage(old_stage) != old_stage:
            new_stage = stage_by_round.get((iv_id, new_sid)) or normalize_stage(old_stage)
        payload = {}
        if new_sid != sid:
            payload["stageId"] = new_sid
        if new_stage != old_stage:
            payload["stage"] = new_stage
        if payload:
            out.append((q["_id"], payload))
    return out


async def run(apply: bool) -> int:
    db = AsyncIOMotorClient(get_settings().MONGODB_URI).get_default_database()
    now = datetime.now(timezone.utc)
    scanned = changed = 0
    rules: Counter = Counter()
    intro_ids: list = []
    remap: dict[tuple, str] = {}
    stage_by_round: dict[tuple, str] = {}

    async for raw in db.interviews.find({}):
        scanned += 1
        res = resolve_interview(raw, now=now)
        payload = changed_fields(raw, res.doc)
        rules.update(res.rules)
        for old, new in res.question_remap.items():
            remap[(raw["_id"], old)] = new
        for entry in res.doc.get("stageHistory") or []:
            if entry.get("id"):
                stage_by_round[(raw["_id"], entry["id"])] = entry.get("stage")
        if res.rules.get("rejected_only_to_intro"):
            intro_ids.append(raw["_id"])
        if not payload:
            continue
        changed += 1
        if apply:
            await db.interviews.update_one({"_id": raw["_id"]}, {"$set": payload})

    questions = await db.interviewquestions.find(
        {}, {"interviewId": 1, "stageId": 1, "stage": 1},
    ).to_list(None)
    q_updates = question_updates(questions, remap, stage_by_round)
    if apply:
        for qid, q_payload in q_updates:
            await db.interviewquestions.update_one({"_id": qid}, {"$set": q_payload})
    relinked = sum(1 for _, q in q_updates if "stageId" in q)
    stage_before = {q.get("stage") for q in questions} - {None}

    mode = "applied" if apply else "dry-run"
    print(f"mode={mode} scanned={scanned} changed={changed}")
    for name in sorted(rules):
        print(f"  {name}: {rules[name]}")
    print(f"  removed rounds with questions re-pointed: {len(remap)}")
    print(f"  questions updated: {len(q_updates)} (re-linked to previous round: {relinked})")
    new_stage = {qid: p["stage"] for qid, p in q_updates if "stage" in p}
    stage_after = {new_stage.get(q["_id"], q.get("stage")) for q in questions} - {None}
    print(f"  question stage values before: {sorted(stage_before)}")
    print(f"  question stage values after:  {sorted(stage_after)}")
    print(f"  rejected-only -> intro ids ({len(intro_ids)}): {', '.join(str(i) for i in intro_ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(apply="--apply" in sys.argv)))
