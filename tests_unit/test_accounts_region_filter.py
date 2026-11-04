"""GET /accounts?region=: the Profiles list's region filter (in-memory MongoDB via mongomock-motor; route called
directly)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.routers.accounts import list_accounts

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def owner():
    await init_beanie(database=AsyncMongoMockClient()["accounts-region-unit"], document_models=[UserExtra, Account])
    user = await UserExtra(name="Ana", email="ana@x.io").insert()
    for name, region, archived in [
        ("US main", "US", False), ("US old", "US", True), ("EU", "EU", False), ("Asia", "Asia", False),
        ("Nowhere", None, False),
    ]:
        await Account(name=name, region=region, archived=archived, createdBy=user.id).insert()
    return user


async def _names(user, region="", status_="active", search=""):
    out = await list_accounts(user, page=1, limit=10, search=search, userId="", status_=status_, region=region)
    return sorted(a["name"] for a in out["accounts"]), out["pagination"]["total"]


async def test_region_filters_one_region(owner):
    assert await _names(owner, region="US") == (["US main"], 1)


async def test_region_none_lists_profiles_without_a_region(owner):
    assert await _names(owner, region="none") == (["Nowhere"], 1)


async def test_no_region_lists_every_profile(owner):
    assert (await _names(owner))[1] == 4


async def test_region_combines_with_status_and_search(owner):
    assert await _names(owner, region="US", status_="all") == (["US main", "US old"], 2)
    assert await _names(owner, region="US", status_="all", search="old") == (["US old"], 1)


async def test_unknown_region_is_rejected(owner):
    with pytest.raises(HTTPException) as e:
        await _names(owner, region="Mars")
    assert e.value.status_code == 422
