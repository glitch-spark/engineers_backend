"""Bids store and routes against an in-memory MongoDB (mongomock-motor); no real database."""

import os
import re

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


# --- routes ----------------------------------------------------------------------------------

class FakeStorage:
    """Records one entry per folder listing; every window gets `per_window` screenshots."""

    def __init__(self, per_window=4):
        self.per_window, self.listings = per_window, []

    async def count_windows(self, folder_url, windows):
        self.listings.append((folder_url, list(windows)))
        return [self.per_window for _ in windows]

    async def count_bids(self, folder_url, start, end):
        return (await self.count_windows(folder_url, [(start, end)]))[0]


@pytest.fixture
async def api(db, monkeypatch):
    """ASGI client for the bidders router; `api.login(user)` switches the current user."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.bids.live_counts import TtlCache
    from app.config import get_settings
    from app.deps import get_current_user
    from app.routers import bidders

    current = {}
    app = FastAPI()
    app.include_router(bidders.router)
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    storage = FakeStorage()
    monkeypatch.setattr(bidders, "get_bid_storage", lambda: storage)
    monkeypatch.setenv("BID_STORAGE_BUCKET", "bid-screenshots")
    get_settings.cache_clear()
    monkeypatch.setattr(bidders, "CACHE", TtlCache(bidders.CACHE._ttl))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.login = lambda user: current.update(user=user)
        client.storage = storage
        yield client
    get_settings.cache_clear()


def body(**over):
    base = dict(name="Ana Silva", country="br", profileId=None, rate=0.5)
    base.update(over)
    return base


async def test_create_and_list_bidders(api):
    me = await make_user()
    profile = await make_account(me, "AI Full-stack")
    api.login(me)
    res = await api.post("/bidders", json=body(profileId=str(profile.id)))
    assert res.status_code == 200
    created = res.json()["bidder"]
    assert (created["name"], created["country"], created["profileName"]) == ("Ana Silva", "BR", "AI Full-stack")
    assert created["createdAt"].endswith("Z") and created["archivedAt"] is None
    listed = (await api.get("/bidders")).json()["bidders"]
    assert [b["_id"] for b in listed] == [created["_id"]] and listed[0]["profileName"] == "AI Full-stack"


async def test_create_rejects_someone_elses_profile(api):
    me, other = await make_user("Pedro"), await make_user("Maria")
    theirs = await make_account(other)
    api.login(me)
    res = await api.post("/bidders", json=body(profileId=str(theirs.id)))
    assert res.status_code == 422
    assert res.json()["detail"][0]["loc"] == ["body", "profileId"]


async def test_list_active_vs_archived(api):
    me, other = await make_user("Pedro"), await make_user("Maria")
    live = await make_bidder(me, "Ana")
    old = await make_bidder(me, "Old", archived=True)
    await make_bidder(other, "Theirs")
    api.login(me)
    assert [b["_id"] for b in (await api.get("/bidders")).json()["bidders"]] == [str(live.id)]
    archived = (await api.get("/bidders", params={"archived": "true"})).json()["bidders"]
    assert [b["_id"] for b in archived] == [str(old.id)] and archived[0]["archivedAt"].endswith("Z")


async def test_update_bidder(api):
    me = await make_user()
    b = await make_bidder(me, "Ana")
    api.login(me)
    res = await api.put(f"/bidders/{b.id}", json=body(name="Ana S.", rate=0.75, country=""))
    assert res.status_code == 200
    assert (res.json()["name"], res.json()["rate"], res.json()["country"]) == ("Ana S.", 0.75, None)
    stored = await Bidder.get(b.id)
    assert (stored.name, stored.rate) == ("Ana S.", 0.75)


async def test_rate_only_update_keeps_an_archived_profile(api):
    me = await make_user()
    profile = await make_account(me, "Backend")
    b = await make_bidder(me, "Ana", profileId=profile.id)
    profile.archived = True
    await profile.save()
    api.login(me)
    res = await api.put(f"/bidders/{b.id}", json=body(name="Ana", profileId=str(profile.id), rate=0.6))
    assert res.status_code == 200
    assert (res.json()["rate"], res.json()["profileId"], res.json()["profileName"]) == (0.6, str(profile.id), "Backend")
    # Switching to an archived profile is still refused.
    other = await make_bidder(me, "Raj")
    res = await api.put(f"/bidders/{other.id}", json=body(name="Raj", profileId=str(profile.id)))
    assert res.status_code == 422


async def test_archive_is_idempotent(api):
    me = await make_user()
    b = await make_bidder(me, "Ana")
    api.login(me)
    assert (await api.delete(f"/bidders/{b.id}")).status_code == 204
    first = (await Bidder.get(b.id)).archivedAt
    assert first is not None
    assert (await api.delete(f"/bidders/{b.id}")).status_code == 204
    assert (await Bidder.get(b.id)).archivedAt == first


async def test_another_users_bidder_is_not_found(api):
    me, other = await make_user("Pedro"), await make_user("Maria", role="admin")
    mine = await make_bidder(me, "Ana")
    api.login(other)  # admins get no exemption either
    assert (await api.put(f"/bidders/{mine.id}", json=body())).status_code == 404
    assert (await api.delete(f"/bidders/{mine.id}")).status_code == 404
    assert (await api.get(f"/bidders/{mine.id}/reports")).status_code == 404
    assert (await api.get("/bidders/not-an-id/reports")).status_code == 404
    assert (await Bidder.get(mine.id)).archivedAt is None


async def test_reports_history_is_newest_first_and_limited(api):
    me = await make_user()
    b = await make_bidder(me, "Ana")
    for day in range(1, 6):
        await BidReport(**report_row(b.id, f"2026-10-0{day}", count=day, owner=me.id).model_dump()).insert()
    await BidReport(**report_row(b.id, "2026-10-03", kind="weekly", owner=me.id).model_dump()).insert()
    await BidReport(**report_row(PydanticObjectId(), "2026-10-05", owner=me.id).model_dump()).insert()
    api.login(me)
    res = await api.get(f"/bidders/{b.id}/reports", params={"limit": 2})
    reports = res.json()["reports"]
    assert [r["periodKey"] for r in reports] == ["2026-10-05", "2026-10-04"]
    assert reports[0]["periodStart"] == "2026-10-01T23:30:00Z"
    weekly = (await api.get(f"/bidders/{b.id}/reports", params={"kind": "weekly"})).json()["reports"]
    assert [r["periodKey"] for r in weekly] == ["2026-10-03"]
    assert len((await api.get(f"/bidders/{b.id}/reports")).json()["reports"]) == 5
    assert (await api.get(f"/bidders/{b.id}/reports", params={"limit": 201})).status_code == 422
    assert (await api.get(f"/bidders/{b.id}/reports", params={"limit": 0})).status_code == 422


async def test_live_counts_route(api, monkeypatch):
    from app.bids import records
    from app.config import get_settings

    folders = get_settings().model_copy(update={"BID_RECORDS_START": "2099-01-01"})  # every window counts folders
    monkeypatch.setattr(records, "get_settings", lambda: folders)
    me, other = await make_user("Pedro"), await make_user("Maria")
    ana = await make_bidder(me, "Ana")
    await make_bidder(me, "Old", archived=True)
    await make_bidder(other, "Theirs")
    api.login(me)
    res = await api.get("/bidders/live-counts")
    assert res.status_code == 200
    assert res.json() == {str(ana.id): {"today": 4, "week": 4, "error": None}}
    assert [url for url, _ in api.storage.listings] == ["s3://shots/ana/"]  # one listing, both windows
    assert len(api.storage.listings[0][1]) == 2


# --- folders, invites, reset login ---------------------------------------------------------------

async def test_create_assigns_folder_and_returns_an_invite(api):
    me = await make_user(); api.login(me)
    res = (await api.post("/bidders", json=body(name="Puma"))).json()
    assert res["bidder"]["folder"] == "puma"
    assert res["bidder"]["screenshotFolderUrl"] == "s3://bid-screenshots/puma/"
    assert res["bidder"]["status"] == "invited" and res["bidder"]["inviteExpiresAt"].endswith("Z")
    assert re.fullmatch(r"PUMA-\w{4}-\w{4}", res["invite"]["code"])
    second = (await api.post("/bidders", json=body(name="Puma"))).json()
    assert second["bidder"]["folder"] == "puma-2"


async def test_create_without_bucket_is_503(api, monkeypatch):
    from app.config import get_settings
    me = await make_user(); api.login(me)
    monkeypatch.delenv("BID_STORAGE_BUCKET", raising=False)
    monkeypatch.setenv("BID_STORAGE_BUCKET", "")
    get_settings.cache_clear()
    res = await api.post("/bidders", json=body(name="Puma"))
    assert res.status_code == 503 and res.json()["detail"] == "BID_STORAGE_BUCKET is not set"
    assert await Bidder.find().count() == 0


async def test_create_retries_the_folder_on_a_duplicate_key(api, monkeypatch):
    from app.routers import bidders
    me = await make_user(); api.login(me)
    folders, real = [], bidders.unique_folder

    async def racing(name):
        folders.append(await real(name))
        if len(folders) == 1:  # a concurrent request takes the folder after we picked it
            await Bidder(createdBy=me.id, name="Other", rate=1, screenshotFolderUrl="s3://x/puma/", folder="puma").insert()
        return folders[-1]

    monkeypatch.setattr(bidders, "unique_folder", racing)
    res = await api.post("/bidders", json=body(name="Puma"))
    assert res.status_code == 200 and folders == ["puma", "puma-2"]
    assert res.json()["bidder"]["folder"] == "puma-2"


async def test_rename_keeps_the_folder(api):
    me = await make_user(); api.login(me)
    b = (await api.post("/bidders", json=body(name="Puma"))).json()["bidder"]
    res = (await api.put(f"/bidders/{b['_id']}", json=body(name="Puma Cruz", screenshotFolderUrl="s3://x/evil/"))).json()
    assert (res["name"], res["folder"], res["screenshotFolderUrl"]) == ("Puma Cruz", "puma", "s3://bid-screenshots/puma/")


async def test_new_invite_replaces_the_old_code(api):
    me = await make_user(); api.login(me)
    created = (await api.post("/bidders", json=body(name="Puma"))).json()
    bid = created["bidder"]["_id"]
    old_hash = (await Bidder.get(bid)).inviteCodeHash
    res = await api.post(f"/bidders/{bid}/invite")
    assert res.status_code == 200
    assert res.json()["code"] != created["invite"]["code"] and res.json()["expiresAt"].endswith("Z")
    stored = await Bidder.get(bid)
    assert stored.inviteCodeHash and stored.inviteCodeHash != old_hash


async def make_active(owner, name="Puma"):
    return await make_bidder(
        owner, name, folder=name.lower(), username="puma.ana", passwordHash="x",
        registeredAt=datetime.now(timezone.utc),
    )


async def test_new_invite_on_active_is_409(api):
    me = await make_user(); api.login(me)
    b = await make_active(me)
    res = await api.post(f"/bidders/{b.id}/invite")
    assert res.status_code == 409 and res.json()["detail"] == "Already registered — use Reset login"


async def test_reset_login(api):
    me = await make_user(); api.login(me)
    b = await make_active(me)
    res = await api.post(f"/bidders/{b.id}/reset-login")
    assert res.status_code == 200 and set(res.json()) == {"code", "expiresAt"}
    stored = await Bidder.get(b.id)
    assert (stored.username, stored.passwordHash, stored.registeredAt) == (None, None, None)
    assert stored.tokenVersion == 1 and stored.folder == "puma" and stored.inviteCodeHash
    listed = (await api.get("/bidders")).json()["bidders"][0]
    assert listed["status"] == "invited" and listed["username"] is None


async def test_reset_login_on_invited_is_409(api):
    me = await make_user(); api.login(me)
    b = (await api.post("/bidders", json=body(name="Puma"))).json()["bidder"]
    res = await api.post(f"/bidders/{b['_id']}/reset-login")
    assert res.status_code == 409 and res.json()["detail"] == "Not registered yet — use New invite code"


async def test_archive_bumps_token_version_and_clears_invite(api):
    me = await make_user(); api.login(me)
    b = (await api.post("/bidders", json=body(name="Puma"))).json()["bidder"]
    assert (await api.delete(f"/bidders/{b['_id']}")).status_code == 204
    stored = await Bidder.get(b["_id"])
    assert stored.tokenVersion == 1 and stored.inviteCodeHash is None and stored.inviteExpiresAt is None
    assert (await api.delete(f"/bidders/{b['_id']}")).status_code == 204
    assert (await Bidder.get(b["_id"])).tokenVersion == 1


async def test_invite_actions_on_archived_are_409(api):
    me = await make_user(); api.login(me)
    b = await make_bidder(me, "Puma", archived=True, folder="puma", registeredAt=datetime.now(timezone.utc))
    for path in ("invite", "reset-login"):
        res = await api.post(f"/bidders/{b.id}/{path}")
        assert res.status_code == 409 and res.json()["detail"] == "Bidder is archived"


async def test_another_users_bidder_invite_routes_404(api):
    me, other = await make_user("Pedro"), await make_user("Maria")
    mine = await make_bidder(me, "Puma", folder="puma")
    api.login(other)
    for path in ("invite", "reset-login"):
        assert (await api.post(f"/bidders/{mine.id}/{path}")).status_code == 404


async def test_restore_brings_an_archived_bidder_back(api):
    me = await make_user(); api.login(me)
    b = await make_bidder(me, "Puma", archived=True, folder="puma", registeredAt=datetime.now(timezone.utc),
                          username="puma", passwordHash="x", tokenVersion=1)
    res = await api.post(f"/bidders/{b.id}/restore")
    assert res.status_code == 200
    out = res.json()
    assert out["status"] == "active" and out["archivedAt"] is None and out["folder"] == "puma"
    stored = await Bidder.get(b.id)
    # Same login; the token bump from archiving stays, so old extension sessions stay signed out.
    assert stored.archivedAt is None and stored.username == "puma" and stored.tokenVersion == 1
    active = (await api.get("/bidders")).json()["bidders"]
    assert [x["_id"] for x in active] == [str(b.id)]


async def test_restore_unregistered_bidder_is_invited_and_can_get_a_code(api):
    me = await make_user(); api.login(me)
    b = (await api.post("/bidders", json=body(name="Puma"))).json()["bidder"]
    await api.delete(f"/bidders/{b['_id']}")
    out = (await api.post(f"/bidders/{b['_id']}/restore")).json()
    assert out["status"] == "invited" and out["inviteExpiresAt"] is None
    assert (await api.post(f"/bidders/{b['_id']}/invite")).status_code == 200


async def test_restore_active_bidder_is_a_no_op(api):
    me = await make_user(); api.login(me)
    b = await make_bidder(me, "Puma", folder="puma")
    res = await api.post(f"/bidders/{b.id}/restore")
    assert res.status_code == 200 and res.json()["archivedAt"] is None


async def test_restore_another_users_bidder_is_404(api):
    me, other = await make_user("Pedro"), await make_user("Maria")
    mine = await make_bidder(me, "Puma", archived=True, folder="puma")
    api.login(other)
    assert (await api.post(f"/bidders/{mine.id}/restore")).status_code == 404
    assert (await Bidder.get(mine.id)).archivedAt is not None
