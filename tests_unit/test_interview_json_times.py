"""Interview times reach JSON as UTC: Mongo hands back naive datetimes, and a browser reads an offset-less ISO string
as its own local time (the edit form then showed a round hours off)."""

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from beanie import PydanticObjectId  # noqa: E402

from app.models.interview import Interview  # noqa: E402
from app.routers._util import doc_to_dict  # noqa: E402


@pytest.fixture
async def db():
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    database = AsyncMongoMockClient()["interview-json-times"]
    await init_beanie(database=database, document_models=[Interview])
    return database


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def test_round_and_interview_times_serialize_as_utc(db):
    start = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
    iv = await Interview(
        accountId=PydanticObjectId(), createdBy=PydanticObjectId(), scheduledAt=start, endsAt=end,
        stageHistory=[{"stage": "intro", "at": start, "scheduledAt": start, "endsAt": end}],
    ).insert()

    loaded = await Interview.get(iv.id)  # naive, as Mongo returns it
    out = doc_to_dict(loaded)

    entry = out["stageHistory"][0]
    for value, want in (
        (entry["scheduledAt"], start), (entry["endsAt"], end), (entry["at"], start),
        (out["scheduledAt"], start), (out["endsAt"], end),
    ):
        assert value.endswith("Z") or value.endswith("+00:00"), value
        assert _utc(value) == want
    # Only the JSON changes: in Python the values stay naive UTC, as the rest of the code compares them.
    assert loaded.stageHistory[0].scheduledAt.tzinfo is None
