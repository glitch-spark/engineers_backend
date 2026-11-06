"""interview_rounds.merged_rounds: one round enumeration with entry ids (Statistics → Interviews and → Callers)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.interview_rounds import _dated_rounds, merged_rounds
from app.models import Interview, InterviewQuestion

T1, T2, T3 = datetime(2026, 10, 3, 15), datetime(2026, 10, 4, 15), datetime(2026, 10, 10, 15)


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["rounds-merge-unit"], document_models=[Interview, InterviewQuestion])


async def test_consecutive_entries_of_one_stage_are_one_round_with_the_latest_id(db):
    iv = Interview(accountId=PydanticObjectId(), createdBy=PydanticObjectId(), scheduledAt=T3, companyName="Acme",
                   stageHistory=[{"id": "a", "stage": "intro", "at": T1, "scheduledAt": T1, "status": "completed"},
                                 {"id": "b", "stage": "intro", "at": T2, "scheduledAt": T2, "status": "passed"},
                                 {"id": "c", "stage": "tech_round_1", "at": T3, "scheduledAt": T3, "status": "scheduled"}])
    rounds = merged_rounds(iv)
    assert [(r.stage, r.status, r.entry_id) for r in rounds] == [("intro", "passed", "b"), ("tech_round_1", "scheduled", "c")]
    assert rounds[0].at == T2
    assert _dated_rounds(iv) == [(r.stage, r.at, r.status, r.caller) for r in rounds]
