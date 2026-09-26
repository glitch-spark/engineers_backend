"""Move interview-level transcript/note/status onto each interview's latest stage.

Run once after deploying per-stage scripts and notes:
  python -m scripts.migrate_stage_notes          # dry run
  python -m scripts.migrate_stage_notes --apply

Idempotent. For every interview it:
- persists a stable `id` on each stageHistory entry,
- seeds stageHistory from the current stage when it is empty,
- moves top-level transcript/note/status into the tip stage,
- tags existing extracted questions with the tip stage's id.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from beanie import init_beanie  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models import Interview, InterviewQuestion  # noqa: E402
from app.routers.interviews import migrate_legacy_stage_fields  # noqa: E402


async def run(apply: bool) -> int:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    database = client.get_default_database()
    await init_beanie(database=database, document_models=[Interview, InterviewQuestion])

    raw = Interview.get_motor_collection()
    missing_ids: set = set()
    async for doc in raw.find({}, {"stageHistory.id": 1, "stageHistory.stage": 1}):
        if any(not e.get("id") for e in doc.get("stageHistory") or []):
            missing_ids.add(doc["_id"])

    scanned = changed = questions_tagged = 0
    async for iv in Interview.find_all():
        scanned += 1
        moved = migrate_legacy_stage_fields(iv)
        if not moved and iv.id not in missing_ids:
            continue
        changed += 1
        tip = iv.stageHistory[-1] if iv.stageHistory else None
        if not apply:
            continue
        await iv.save()
        if tip is not None:
            res = await InterviewQuestion.find(
                {"interviewId": iv.id, "stageId": None}
            ).update({"$set": {"stageId": tip.id}})
            questions_tagged += getattr(res, "modified_count", 0) or 0

    mode = "applied" if apply else "dry run"
    print(f"[{mode}] scanned {scanned}, changed {changed}, questions tagged {questions_tagged}")
    return 0


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
