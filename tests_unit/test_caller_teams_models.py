import os

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient
from pymongo.errors import DuplicateKeyError

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.models.caller_team import CallerTeam, TeamDayPost, TeamRoundPost  # noqa: E402


@pytest.mark.asyncio
async def test_team_round_post_unique_per_team_and_round():
    await init_beanie(database=AsyncMongoMockClient()["t"], document_models=[CallerTeam, TeamRoundPost, TeamDayPost])
    kw = dict(teamId="t1", interviewId="i1", roundId="r1", channel="C1", startEpoch=1, filesFor=None, state="scheduled")
    await TeamRoundPost(**kw).insert()
    with pytest.raises(DuplicateKeyError):
        await TeamRoundPost(**kw).insert()
    await TeamRoundPost(**{**kw, "teamId": "t2"}).insert()
