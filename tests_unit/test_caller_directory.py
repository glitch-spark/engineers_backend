"""Caller directory: active platform users + external callers, mongomock."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.caller_directory import list_directory, resolve_caller, user_caller_color, user_display_name
from app.models import Caller, UserExtra
from app.models.caller import CALLER_COLORS

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["caller-dir"]
    await init_beanie(database=database, document_models=[UserExtra, Caller])
    return database


async def _seed():
    ana = await UserExtra(name="Ana", email="ana@x.com", role="staff").insert()
    bo = await UserExtra(name="Bo", email="bo@x.com", isActive=False).insert()
    muhammad = await Caller(name="Muhammad", nameKey="muhammad").insert()
    old = await Caller(name="Old", nameKey="old", archivedAt=datetime(2026, 11, 1, tzinfo=timezone.utc)).insert()
    return ana, bo, muhammad, old


async def test_directory_lists_active_users_then_external(db):
    await _seed()
    entries = await list_directory()
    assert [(e["kind"], e["name"]) for e in entries] == [("user", "Ana"), ("external", "Muhammad")]
    assert [e["name"] for e in await list_directory(include_archived=True)] == ["Ana", "Muhammad", "Old"]
    archived = {e["name"]: e["archived"] for e in await list_directory(include_archived=True)}
    assert archived == {"Ana": False, "Muhammad": False, "Old": True}


async def test_directory_includes_every_role_and_names_fall_back(db):
    await UserExtra(name=None, email="zed@x.com", role="admin").insert()
    await UserExtra(name="Mia", email="mia@x.com", role="interview_manager").insert()
    names = [e["name"] for e in await list_directory()]
    assert names == ["Mia", "zed@x.com"]


def test_user_color_is_stable_and_override_wins():
    assert user_caller_color("6ad7bb33695b5819321f5785", None) == user_caller_color("6ad7bb33695b5819321f5785", None)
    assert user_caller_color("6ad7bb33695b5819321f5785", "rose") == "rose"
    assert user_caller_color("6ad7bb33695b5819321f5785", "not-a-colour") in CALLER_COLORS


def test_user_display_name():
    assert user_display_name(UserExtra(name="Ana", email="a@x.com")) == "Ana"
    assert user_display_name(UserExtra(name=" ", email="a@x.com")) == "a@x.com"
    assert user_display_name(UserExtra()) == "User"


async def test_resolve_finds_users_and_externals_but_not_inactive_or_archived(db):
    ana, bo, muhammad, old = await _seed()
    assert (await resolve_caller(str(ana.id)))["kind"] == "user"
    assert (await resolve_caller(str(muhammad.id)))["kind"] == "external"
    assert await resolve_caller(str(bo.id)) is None and await resolve_caller(str(old.id)) is None
    assert await resolve_caller("not-an-id") is None
    assert await resolve_caller("6ad7bb33695b5819321f5785") is None
