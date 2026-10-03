"""Bidder folder slugs, assignment and backfill against an in-memory MongoDB."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids.folders import backfill_folders, folder_url, profile_segment, slug, unique_folder
from app.models import UserExtra
from app.models.bidder import Bidder

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals, not bids code
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["bids-folders-unit"]
    await init_beanie(database=database, document_models=[Bidder, UserExtra])
    return database


async def make_user(name="Pedro") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role="staff").insert()


async def make_bidder(owner: UserExtra, name="Ana", archived=False, **kw) -> Bidder:
    kw.setdefault("screenshotFolderUrl", f"s3://shots/{name.lower()}/")
    return await Bidder(
        createdBy=owner.id, name=name, rate=0.5,
        archivedAt=datetime.now(timezone.utc) if archived else None, **kw,
    ).insert()


@pytest.mark.parametrize("text,expected", [
    ("Puma", "puma"), ("Ana Silva", "ana-silva"), ("José Ñandú", "jose-nandu"),
    ("  --Ana__2!! ", "ana-2"), ("", "bidder"), ("!!!", "bidder"), ("x" * 50, "x" * 40),
    ("a" * 39 + " b", "a" * 39),          # cut at 40 leaves a trailing "-", re-trimmed
])
def test_slug(text, expected):
    assert slug(text) == expected


def test_profile_segment():
    assert profile_segment("Upwork") == "upwork"
    assert profile_segment("AI Full-stack") == "ai-full-stack"
    assert profile_segment(None) == "no-profile" and profile_segment("!!") == "no-profile"


def test_folder_url():
    assert folder_url("bid-screenshots", "ana") == "s3://bid-screenshots/ana/"


async def test_unique_folder_appends_a_suffix(db):     # archived bidders still hold their folder
    owner = await make_user()
    await make_bidder(owner, "Ana", folder="ana", archived=True)
    await make_bidder(owner, "Ana 2", folder="ana-2")
    assert await unique_folder("Ana") == "ana-3"
    assert await unique_folder("Puma") == "puma"


async def test_backfill_takes_the_folder_from_legacy_urls(db):
    owner = await make_user()
    puma = await make_bidder(owner, "Puma", screenshotFolderUrl="s3://bid-screenshots/puma/")
    nested = await make_bidder(owner, "Ana", screenshotFolderUrl="s3://shots/bids/ana/")
    clash = await make_bidder(owner, "Puma B", screenshotFolderUrl="https://f005.backblazeb2.com/file/b/puma/")
    done = await make_bidder(owner, "Raj", folder="raj")
    assert await backfill_folders() == 3
    got = {b.id: b.folder for b in await Bidder.find({}).to_list()}
    assert (got[puma.id], got[nested.id], got[clash.id], got[done.id]) == ("puma", "bids-ana", "puma-2", "raj")
    assert await backfill_folders() == 0                 # idempotent


async def test_backfill_falls_back_to_the_name_for_unparseable_urls(db):
    owner = await make_user()
    b = await make_bidder(owner, "José Ñandú", screenshotFolderUrl="not a url")
    assert await backfill_folders() == 1
    assert (await Bidder.get(b.id)).folder == "jose-nandu"


async def test_unset_login_fields_are_not_stored_so_sparse_indexes_ignore_them(db):
    owner = await make_user()
    await make_bidder(owner, "A")
    await make_bidder(owner, "B")                          # a null in a sparse unique index would collide
    raw = await db["bidders"].find_one({"name": "A"})
    assert not {"folder", "username", "inviteCodeHash", "passwordHash"} & set(raw)
    b = await Bidder.find_one(Bidder.name == "A")
    b.username = "ana"
    await b.save()
    b.username = None
    await b.save()                                         # clearing removes the key
    assert "username" not in await db["bidders"].find_one({"name": "A"})
