"""Response window (median / p90 of days to interview) against mongomock; no real database."""

from datetime import date, datetime, timedelta

import pytest
from beanie import init_beanie
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.applications import window
from app.models import Application

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

TODAY = date(2026, 11, 18)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["window-unit"]
    await init_beanie(database=database, document_models=[Application])
    window.clear_cache()
    return database


async def add_rows(db, days, age=10):
    """One matched row per entry of `days` (daysToInterview), applied `age` days before TODAY."""
    at = datetime(2026, 11, 18) - timedelta(days=age)
    await db["applications"].insert_many([
        {"key": f"k{ObjectId()}", "ownerId": ObjectId(), "appliedAt": at, "interviewId": ObjectId(),
         "daysToInterview": d, "sources": []} for d in days])


async def test_window_median_p90(db):
    await add_rows(db, range(1, 41))  # 1..40: nearest-rank median = 20th value, p90 = 36th
    await add_rows(db, [99], age=100)  # outside the 90 days
    await db["applications"].insert_one({"key": "nomatch", "ownerId": ObjectId(), "appliedAt": datetime(2026, 11, 8)})
    w = await window.response_window(TODAY)
    assert w == {"median": 20, "p90": 36, "matured_days": 36, "n": 40, "thin": False}


async def test_window_thin_fallback(db):
    await add_rows(db, range(1, 30))  # 29 < 30
    w = await window.response_window(TODAY)
    assert w == {"median": None, "p90": 14, "matured_days": 14, "n": 29, "thin": True}


async def test_matured_never_below_7(db):
    await add_rows(db, [0, 1, 2] * 10)
    w = await window.response_window(TODAY)
    assert w["p90"] == 2 and w["matured_days"] == 7 and not w["thin"]


async def test_cache_reused_within_10_minutes(db):
    await add_rows(db, [1] * 30)
    first = await window.response_window(TODAY)
    await add_rows(db, [50] * 30)
    assert await window.response_window(TODAY) == first
    window.clear_cache()
    assert (await window.response_window(TODAY))["p90"] == 50


async def test_window_excludes_by_interview_only_rows(db):
    """C1: rows that exist only because they got an interview are dropped here too."""
    await add_rows(db, [1] * 30)
    at = datetime(2026, 11, 8)
    await db["applications"].insert_many([
        {"key": f"f{ObjectId()}", "ownerId": ObjectId(), "appliedAt": at, "interviewId": ObjectId(),
         "daysToInterview": 50, "sources": [], "byInterviewOnly": True} for _ in range(30)])
    w = await window.response_window(TODAY)
    assert w["n"] == 30 and w["median"] == 1
