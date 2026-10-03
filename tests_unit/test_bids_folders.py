"""Bidder folder slugs, assignment and backfill against an in-memory MongoDB."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids.folders import (
    backfill_folders, folder_url, format_report, print_report, profile_segment, report_rows, slug, unique_folder,
)
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


@pytest.fixture(autouse=True)
def bucket(monkeypatch):
    """Pin BID_STORAGE_BUCKET: folder assignment looks at legacy URLs in it (the local .env may set another)."""
    from app.config import get_settings

    monkeypatch.setenv("BID_STORAGE_BUCKET", "bid-screenshots")
    get_settings.cache_clear()
    yield "bid-screenshots"
    get_settings.cache_clear()


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


async def test_unique_folder_skips_top_level_prefixes_of_legacy_urls_in_the_bucket(db):
    owner = await make_user()
    await make_bidder(owner, "Ana", folder="bids-ana", screenshotFolderUrl="s3://bid-screenshots/bids/ana/")
    await make_bidder(owner, "Zed", folder="zed-old", screenshotFolderUrl="s3://other-bucket/zed/")
    assert await unique_folder("Bids") == "bids-2"     # bids/ holds Ana's legacy screenshots
    assert await unique_folder("Zed") == "zed"         # another bucket's prefixes are no clash


async def test_unique_folder_ignores_urls_without_a_bucket_setting(db, monkeypatch):
    from app.config import get_settings

    owner = await make_user()
    await make_bidder(owner, "Ana", folder="bids-ana", screenshotFolderUrl="s3://bid-screenshots/bids/ana/")
    monkeypatch.setenv("BID_STORAGE_BUCKET", "")
    get_settings.cache_clear()
    assert await unique_folder("Bids") == "bids"


async def test_backfill_lets_the_bidder_stored_in_the_bucket_keep_its_folder(db):
    owner = await make_user()
    elsewhere = await make_bidder(owner, "Puma Old", screenshotFolderUrl="s3://old-bucket/puma/")  # created first
    home = await make_bidder(owner, "Puma", screenshotFolderUrl="s3://bid-screenshots/puma/")
    console = await make_bidder(owner, "Raj", screenshotFolderUrl="https://f005.backblazeb2.com/file/bid-screenshots/raj/")
    early = await make_bidder(owner, "Raj Old", screenshotFolderUrl="s3://old-bucket/raj/")
    assert await backfill_folders() == 4
    got = {b.id: b.folder for b in await Bidder.find({}).to_list()}
    assert (got[home.id], got[elsewhere.id]) == ("puma", "puma-2")
    assert (got[console.id], got[early.id]) == ("raj", "raj-2")   # any URL form of the same bucket folder


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


async def test_report_lists_bidders_whose_counting_moves_on_registration(db):
    owner = await make_user()
    await make_bidder(owner, "Home", folder="home", screenshotFolderUrl="s3://bid-screenshots/home/")
    await make_bidder(owner, "Console", folder="console",
                      screenshotFolderUrl="https://f005.backblazeb2.com/file/bid-screenshots/console/")
    await make_bidder(owner, "Puma Old", folder="puma-2", screenshotFolderUrl="s3://old-bucket/puma/")
    await make_bidder(owner, "Nested", folder="bids-ana", screenshotFolderUrl="s3://bid-screenshots/bids/ana/")
    await make_bidder(owner, "Gone", folder="gone", screenshotFolderUrl="s3://old-bucket/gone/", archived=True)
    before = await db["bidders"].find({}).to_list(None)
    rows = await report_rows("bid-screenshots")
    assert rows == [
        ("Puma Old", "puma-2", "s3://old-bucket/puma/"),
        ("Nested", "bids-ana", "s3://bid-screenshots/bids/ana/"),
    ]
    assert await db["bidders"].find({}).to_list(None) == before   # read-only


def test_format_report():
    assert format_report([("Puma Old", "puma-2", "s3://old-bucket/puma/")]).splitlines() == [
        "1 bidder will switch counting to a new folder when they register:",
        "name\tfolder\tcurrent URL",
        "Puma Old\tpuma-2\ts3://old-bucket/puma/",
    ]
    assert format_report([]) == "No bidder switches folders when they register."


async def test_print_report(db, capsys, monkeypatch):
    from app.config import get_settings

    owner = await make_user()
    await make_bidder(owner, "Puma Old", folder="puma-2", screenshotFolderUrl="s3://old-bucket/puma/")
    assert await print_report(database=db) == 0
    assert "Puma Old\tpuma-2\ts3://old-bucket/puma/" in capsys.readouterr().out
    monkeypatch.setenv("BID_STORAGE_BUCKET", "")
    get_settings.cache_clear()
    assert await print_report(database=db) == 2
    assert "BID_STORAGE_BUCKET is not set" in capsys.readouterr().err
