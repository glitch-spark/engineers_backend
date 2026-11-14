import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.routers import bids as bids_router

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

KEYS = {"period", "start", "end", "label", "recordsStart", "weeks", "bidders", "totals", "top", "payWeek"}


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["performance-router-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, Interview, InterviewQuestion])
    monkeypatch.setattr(bids_router, "_now", lambda: datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc))
    return database


async def make_user(name, role="staff") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def make_bidder(owner, name, archived_at=None) -> Bidder:
    return await Bidder(
        createdBy=owner.id, name=name, rate=0.5, folder=name.lower(), screenshotFolderUrl=f"s3://shots/{name.lower()}/",
        archivedAt=archived_at, createdAt=datetime(2026, 1, 1),
    ).insert()


async def call(user, period="month", date="", to="", includeArchived=False, ownerId=None):
    return await bids_router.bids_performance(
        user, period=period, date=date, to=to, includeArchived=includeArchived, ownerId=ownerId)


async def test_shape(db):
    staff = await make_user("Pedro")
    await make_bidder(staff, "Ana")
    r = await call(staff, date="2026-10-15")
    assert set(r) == KEYS
    assert (r["period"], r["start"], r["end"]) == ("month", "2026-10-01", "2026-10-31")


async def test_visibility(db):
    pedro, maria = await make_user("Pedro"), await make_user("Maria")
    admin = await make_user("Root", "admin")
    await make_bidder(pedro, "Ana")
    await make_bidder(maria, "Bo")
    await make_bidder(maria, "Old", archived_at=datetime(2026, 10, 10))

    names = lambda r: [b["name"] for b in r["bidders"]]
    assert names(await call(pedro)) == ["Ana"]
    assert names(await call(pedro, ownerId=str(maria.id))) == []
    assert names(await call(admin)) == ["Ana", "Bo"]
    assert names(await call(admin, ownerId=str(maria.id))) == ["Bo"]
    assert names(await call(admin, includeArchived=True)) == ["Ana", "Bo", "Old"]


async def test_current_month_ends_today(db):
    staff = await make_user("Pedro")
    r = await call(staff)  # no date: the month holding today (Wed Nov 4)
    assert (r["start"], r["end"]) == ("2026-11-01", "2026-11-04")
    r = await call(staff, period="week")
    assert (r["start"], r["end"]) == ("2026-11-01", "2026-11-04")  # the pay week runs Sun Nov 1 .. Sat Nov 7
    r = await call(staff, date="2026-10-15")  # a past month is whole
    assert r["end"] == "2026-10-31"


async def test_bad_period_422(db):
    staff = await make_user("Pedro")
    with pytest.raises(HTTPException) as e:
        await call(staff, period="year", date="not-a-date")
    assert e.value.status_code == 422


async def test_bad_range_400(db):
    staff = await make_user("Pedro")
    for kwargs in ({"date": "nope"}, {"period": "custom", "date": "2026-10-10"},
                   {"period": "custom", "date": "2026-10-10", "to": "2026-10-01"}):
        with pytest.raises(HTTPException) as e:
            await call(staff, **kwargs)
        assert e.value.status_code == 400
