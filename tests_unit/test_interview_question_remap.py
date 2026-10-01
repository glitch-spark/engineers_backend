"""Question re-linking when an unmigrated interview is loaded and saved (no database)."""

import asyncio
import os
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import init_beanie  # noqa: E402
from bson import ObjectId  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.interview_normalize import pending_question_updates  # noqa: E402
from app.models import Interview, InterviewQuestion  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _beanie_offline():
    # Unreachable server: motor connects lazily, skip_indexes avoids index I/O,
    # and the one buildInfo version probe init_beanie makes is stubbed.
    client = AsyncIOMotorClient("mongodb://127.0.0.1:1/unit-test", serverSelectionTimeoutMS=50)
    db = client.get_default_database()

    async def _build_info(*_args, **_kwargs):
        return {"version": "7.0.0"}

    db.command = _build_info
    asyncio.run(init_beanie(
        database=db,
        document_models=[Interview, InterviewQuestion],
        skip_indexes=True,
    ))


def _raw_with_rejected_tip():
    when = datetime(2026, 9, 1)
    return {
        "_id": ObjectId(),
        "accountId": ObjectId(),
        "createdBy": ObjectId(),
        "scheduledAt": when,
        "stage": "rejected",
        "status": "completed",
        "stageHistory": [
            {"id": "a", "stage": "intro", "status": "completed", "at": when, "scheduledAt": when},
            {"id": "b", "stage": "rejected", "status": "completed", "at": when, "scheduledAt": when},
        ],
    }


def test_loading_unmigrated_interview_keeps_question_remap():
    iv = Interview.model_validate(_raw_with_rejected_tip())
    assert [e.stage for e in iv.stageHistory] == ["intro"]
    assert iv.pending_question_remap == {"b": "a"}


def test_migrated_interview_has_no_pending_remap():
    raw = _raw_with_rejected_tip()
    raw["stageHistory"] = raw["stageHistory"][:1]
    raw["stage"] = "intro"
    assert Interview.model_validate(raw).pending_question_remap == {}


def test_pending_question_updates_relinks_and_restages():
    iv = SimpleNamespace(
        id="iv1",
        pending_question_remap={"b": "a"},
        stageHistory=[SimpleNamespace(id="a", stage="intro")],
    )
    questions = [
        {"_id": 1, "interviewId": "iv1", "stageId": "b", "stage": "rejected"},
        {"_id": 2, "interviewId": "iv1", "stageId": "a", "stage": "intro"},
    ]
    assert pending_question_updates(iv, questions) == [(1, {"stageId": "a", "stage": "intro"})]
