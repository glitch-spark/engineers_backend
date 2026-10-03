"""Bids store and routes against an in-memory MongoDB (mongomock-motor); no real database."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids.reports import BeanieReportStore
from app.models import Account, UserExtra
from app.models.bid_report import BidReport, BidReportRow
from app.models.bidder import Bidder
from app.models.slack_alert_log import SlackAlertLog

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals, not bids code
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["bids-unit"]  # fresh in-memory database per test
    await init_beanie(database=database, document_models=[Bidder, BidReport, Account, UserExtra, SlackAlertLog])
    return database


async def make_user(name="Pedro", role="staff", slack=None) -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, slackUserId=slack).insert()


async def make_account(owner: UserExtra, name="Backend", archived=False) -> Account:
    return await Account(name=name, createdBy=owner.id, archived=archived).insert()


async def make_bidder(owner: UserExtra, name="Ana", archived=False, **kw) -> Bidder:
    return await Bidder(
        createdBy=owner.id, name=name, rate=0.5, screenshotFolderUrl=f"s3://shots/{name.lower()}/",
        archivedAt=datetime.now(timezone.utc) if archived else None, **kw,
    ).insert()


def report_row(bidder_id, period_key="2026-10-02", kind="daily", count=3, owner=None) -> BidReportRow:
    return BidReportRow(
        kind=kind, periodKey=period_key, periodStart=START, periodEnd=END, bidderId=bidder_id,
        ownerId=owner or PydanticObjectId(), bidderName="Ana", rate=0.5, count=count, amount=count * 0.5,
    )


# --- indexes ---------------------------------------------------------------------------------

async def test_report_indexes_include_the_unique_one(db):
    info = await db["bidreports"].index_information()
    keys = {name: (list(ix["key"]), bool(ix.get("unique"))) for name, ix in info.items() if name != "_id_"}
    assert sorted(keys.values()) == [
        ([("bidderId", 1), ("kind", 1), ("periodKey", 1)], True),
        ([("kind", 1), ("periodKey", 1)], False),
    ]


async def test_duplicate_report_rows_are_rejected(db):
    bid = PydanticObjectId()
    await BidReport(**report_row(bid).model_dump()).insert()
    from pymongo.errors import DuplicateKeyError
    with pytest.raises(DuplicateKeyError):
        await BidReport(**report_row(bid, count=9).model_dump()).insert()


# --- BeanieReportStore -----------------------------------------------------------------------

async def test_claim_and_release(db):
    store = BeanieReportStore()
    assert await store.claim("bids-daily:2026-10-02")
    assert not await store.claim("bids-daily:2026-10-02")
    assert await store.claim("bids-weekly:2026-10-02")  # other keys are independent
    await store.release("bids-daily:2026-10-02")
    assert await store.claim("bids-daily:2026-10-02")


async def test_save_rows_then_load_rows(db):
    store = BeanieReportStore()
    a, b = PydanticObjectId(), PydanticObjectId()
    saved = await store.save_rows([report_row(a), report_row(b, count=5)])
    assert len(saved) == 2
    await store.save_rows([report_row(a, kind="weekly")])
    loaded = await store.load_rows("daily", "2026-10-02")
    assert sorted(r.count for r in loaded) == [3, 5]
    assert await store.load_rows("daily", "2026-10-03") == []
    assert await store.save_rows([]) == []


async def test_save_rows_falls_back_to_the_rows_another_run_saved(db):
    store = BeanieReportStore()
    a = PydanticObjectId()
    await store.save_rows([report_row(a, count=3)])
    got = await store.save_rows([report_row(a, count=99)])  # a concurrent run lost the race
    assert [r.count for r in got] == [3]


async def test_save_rows_reraises_non_duplicate_bulk_errors(db, monkeypatch):
    from pymongo.errors import BulkWriteError

    async def boom(*a, **kw):
        raise BulkWriteError({"writeErrors": [{"index": 0, "code": 11000}, {"index": 1, "code": 121}]})

    monkeypatch.setattr(BidReport, "insert_many", boom)
    with pytest.raises(BulkWriteError):
        await BeanieReportStore().save_rows([report_row(PydanticObjectId()), report_row(PydanticObjectId())])


async def test_active_bidders_excludes_archived(db):
    me = await make_user()
    live = await make_bidder(me, "Ana")
    await make_bidder(me, "Old", archived=True)
    assert [b.id for b in await BeanieReportStore().active_bidders()] == [live.id]


async def test_owners_and_profile_names(db):
    store = BeanieReportStore()
    pedro = await make_user("Pedro", slack="U123")
    maria = await make_user("Maria")
    account = await make_account(pedro, "AI Full-stack")
    owners = await store.owners([pedro.id, maria.id])
    assert owners[str(pedro.id)].label == "<@U123>" and owners[str(pedro.id)].name == "Pedro"
    assert owners[str(maria.id)].label == "Maria"
    assert await store.owners([]) == {}
    assert await store.profile_names([account.id]) == {str(account.id): "AI Full-stack"}
    assert await store.profile_names([]) == {}
