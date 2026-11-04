"""Daily Plan self bids per profile (in-memory MongoDB via mongomock-motor): typed per profile, the region
fields are derived from each profile's region at save time."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, DailyPlan, Interview, InterviewQuestion, UserExtra, WeeklyPlan
from app.plan_profiles import derive_self_bids
from app.routers.daily_plans import get_day, list_week, put_day

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

DAY = "2026-10-21"  # a Wednesday: week Oct 19–25


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["daily-plan-by-profile-unit"]
    await init_beanie(
        database=database,
        document_models=[UserExtra, Account, DailyPlan, WeeklyPlan, Interview, InterviewQuestion],
    )


@pytest.fixture
async def owner(db):
    return await UserExtra(name="Ana", email="ana@x.io").insert()


async def _profile(user, name, region):
    return await Account(name=name, createdBy=user.id, region=region).insert()


async def _stored():
    return (await DailyPlan.find_all().to_list())[0].done


def test_derive_self_bids():
    regions = {"a": "US", "b": "EU", "c": "Latam", "d": "Canada"}
    assert derive_self_bids({"a": 5, "b": 3, "c": 2, "d": 4}, regions) == {
        "bidsSelf": 14, "bidsSelfUs": 5, "bidsSelfEu": 3, "bidsSelfLatam": 2}
    assert derive_self_bids({"a": 5, "x": 1}, {"a": "US", "x": None})["bidsSelf"] == 6


async def test_put_by_profile_derives_regions(owner):
    us, asia = await _profile(owner, "US one", "US"), await _profile(owner, "Asia one", "Asia")
    by = {str(us.id): 5, str(asia.id): 4}
    out = await put_day(DAY, owner, {"done": {"bidsSelfByProfile": by, "bidsBidder": 2}})
    done = await _stored()
    assert done.bidsSelfByProfile == by
    assert (done.bidsSelf, done.bidsSelfUs, done.bidsBidder) == (9, 5, 2)
    assert out["done"]["bidsSelfByProfile"] == by


async def test_put_drops_zero_entries(owner):
    us, eu = await _profile(owner, "US one", "US"), await _profile(owner, "EU one", "EU")
    await put_day(DAY, owner, {"done": {"bidsSelfByProfile": {str(us.id): 5, str(eu.id): 0}}})
    assert (await _stored()).bidsSelfByProfile == {str(us.id): 5}


async def test_put_rejects_foreign_profile(owner):
    other = await UserExtra(name="Bo", email="bo@x.io").insert()
    theirs = await _profile(other, "Theirs", "US")
    with pytest.raises(HTTPException) as err:
        await put_day(DAY, owner, {"done": {"bidsSelfByProfile": {str(theirs.id): 3}}})
    assert err.value.status_code == 422


async def test_region_fields_fixed_at_save(owner):
    us = await _profile(owner, "US one", "US")
    await put_day(DAY, owner, {"done": {"bidsSelfByProfile": {str(us.id): 5}}})
    us.region = "EU"
    await us.save()
    assert (await get_day(DAY, owner, userId=None))["done"]["bidsSelfUs"] == 5


async def test_put_region_only_unchanged(owner):
    await put_day(DAY, owner, {"done": {"bidsSelfUs": 3, "bidsSelfEu": 2}})
    done = await _stored()
    assert (done.bidsSelf, done.bidsSelfByProfile) == (5, {})


async def test_day_profiles_are_owners(owner):
    admin = await UserExtra(name="Ad", email="ad@x.io", role="admin").insert()
    mine = await _profile(owner, "Mine", "US")
    await _profile(admin, "Not mine", "EU")
    out = await get_day(DAY, admin, userId=str(owner.id))
    assert [p["id"] for p in out["profiles"]] == [str(mine.id)]


async def test_week_includes_profiles(owner):
    us = await _profile(owner, "US one", "US")
    await put_day(DAY, owner, {"done": {"bidsSelfByProfile": {str(us.id): 5}}})
    out = await list_week(owner, weekStart="2026-10-19", userId=None)
    assert [p["id"] for p in out["profiles"]] == [str(us.id)]
    assert all("profiles" not in d for d in out["days"])
    assert out["days"][2]["done"]["bidsSelfByProfile"] == {str(us.id): 5}


async def test_put_drops_deleted_profile(owner):
    us, eu = await _profile(owner, "US one", "US"), await _profile(owner, "EU one", "EU")
    await put_day(DAY, owner, {"done": {"bidsSelfByProfile": {str(us.id): 5, str(eu.id): 3}}})
    stored = (await _stored()).bidsSelfByProfile
    await eu.delete()
    out = await put_day(DAY, owner, {"done": {"bidsSelfByProfile": stored, "bidsSelf": 8, "bidsSelfEu": 3}})
    done = await _stored()
    assert done.bidsSelfByProfile == {str(us.id): 5}
    assert (done.bidsSelf, done.bidsSelfUs, done.bidsSelfEu) == (5, 5, 0)
    assert out["done"]["bidsSelfByProfile"] == {str(us.id): 5}


async def test_put_still_rejects_foreign_profile_beside_deleted(owner):
    other = await UserExtra(name="Bo", email="bo@x.io").insert()
    theirs, gone = await _profile(other, "Theirs", "US"), await _profile(owner, "Gone", "EU")
    await gone.delete()
    with pytest.raises(HTTPException) as err:
        await put_day(DAY, owner, {"done": {"bidsSelfByProfile": {str(gone.id): 2, str(theirs.id): 3}}})
    assert err.value.status_code == 422


async def test_put_zero_map_keeps_region_total(owner):
    us = await _profile(owner, "US one", "US")
    await put_day(DAY, owner, {"done": {"bidsSelfByProfile": {str(us.id): 0}, "bidsSelfUs": 3}})
    done = await _stored()
    assert (done.bidsSelf, done.bidsSelfUs, done.bidsSelfByProfile) == (3, 3, {})
