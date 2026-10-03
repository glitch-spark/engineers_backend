"""Extension routes (/ext) against an in-memory MongoDB; real bidder tokens, no auth override on /ext."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.bid_report import BidReport
from app.models.bidder import Bidder
from app.models.slack_alert_log import SlackAlertLog

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

BAD = "Wrong username or password"
INVALID = "Invite code is invalid or expired"


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["ext-unit"]
    await init_beanie(database=database, document_models=[Bidder, BidReport, Account, UserExtra, SlackAlertLog])
    return database


@pytest.fixture
async def owner(db):
    return await UserExtra(name="Pedro", email="pedro@x.io", role="staff").insert()


@pytest.fixture
async def ext(db, owner, monkeypatch):
    """One app with owner routes (owner override) and /ext routes (real auth). `ext.create(name)` -> (id, code)."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.bids.rate_limit import FailureLimiter
    from app.config import get_settings
    from app.deps import get_current_user
    from app.routers import bidder_ext, bidders

    app = FastAPI()
    app.include_router(bidders.router)
    app.include_router(bidder_ext.router)
    app.dependency_overrides[get_current_user] = lambda: owner
    monkeypatch.setenv("BID_STORAGE_BUCKET", "bid-screenshots")
    get_settings.cache_clear()
    monkeypatch.setattr(bidder_ext, "LIMITER", FailureLimiter())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async def create(name="Ana Lima"):
            r = await client.post("/bidders", json={"name": name, "rate": 0.5})
            assert r.status_code == 200, r.text
            return r.json()["bidder"]["_id"], r.json()["invite"]["code"]

        client.create = create
        yield client
    get_settings.cache_clear()


def reg(code, username="ana", password="password1"):
    return {"inviteCode": code, "username": username, "password": password}


async def register(ext, name="Ana Lima", username="ana", password="password1"):
    bid, code = await ext.create(name)
    r = await ext.post("/ext/register", json=reg(code, username, password))
    assert r.status_code == 200, r.text
    return bid, code, r.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def test_register_login_me(ext):
    bid, _, body = await register(ext)
    assert body["bidder"] == {
        "name": "Ana Lima", "folder": "ana-lima", "profileName": None, "username": "ana", "status": "active",
    }
    me = await ext.get("/ext/me", headers=auth(body["token"]))
    assert me.status_code == 200 and me.json() == body["bidder"]
    login = await ext.post("/ext/login", json={"username": "ANA", "password": "password1"})
    assert login.status_code == 200
    assert (await ext.get("/ext/me", headers=auth(login.json()["token"]))).json()["status"] == "active"
    stored = await Bidder.get(PydanticObjectId(bid))
    assert stored.passwordHash and stored.passwordHash != "password1"
    assert stored.inviteCodeHash is None and stored.inviteExpiresAt is None


async def test_register_accepts_a_sloppily_typed_code(ext):
    _, code = await ext.create()
    tag, a, b = code.split("-")
    sloppy = f"{tag.lower()} {a.lower().replace('0', 'o')} {b.lower().replace('0', 'o')}"
    r = await ext.post("/ext/register", json=reg(sloppy))
    assert r.status_code == 200, r.text


async def test_register_code_is_single_use(ext):
    _, code = await ext.create()
    assert (await ext.post("/ext/register", json=reg(code))).status_code == 200
    r = await ext.post("/ext/register", json=reg(code, username="other"))
    assert r.status_code == 400 and r.json()["detail"] == INVALID


async def test_register_expired_code(ext):
    bid, code = await ext.create()
    b = await Bidder.get(PydanticObjectId(bid))
    b.inviteExpiresAt = datetime.now(timezone.utc) - timedelta(minutes=1)
    await b.save()
    r = await ext.post("/ext/register", json=reg(code))
    assert r.status_code == 400 and r.json()["detail"] == INVALID


async def test_register_wrong_code(ext):
    r = await ext.post("/ext/register", json=reg("ZZZZ-AAAA-BBBB"))
    assert r.status_code == 400 and r.json()["detail"] == INVALID


async def test_register_rejects_bad_credentials(ext):
    _, code = await ext.create()
    for body in (reg(code, "ab"), reg(code, "Bad Name!"), reg(code, "ana", "1234567")):
        r = await ext.post("/ext/register", json=body)
        assert r.status_code == 422, body
        assert r.json()["detail"][0]["loc"][0] == "body"
    await register(ext, name="Bia Souza", username="taken")
    r = await ext.post("/ext/register", json=reg(code, "taken"))
    assert r.status_code == 409 and r.json()["detail"] == "Username is taken"
    # the code was not consumed by the failed attempt
    assert (await ext.post("/ext/register", json=reg(code, "free"))).status_code == 200


async def test_register_is_one_conditional_update(ext):
    """If the owner issues a new code between our read and write, the write matches nothing -> 400."""
    from app.routers import bidder_ext

    bid, code = await ext.create()
    stale = await Bidder.get(PydanticObjectId(bid))
    new_code = (await ext.post(f"/bidders/{bid}/invite")).json()["code"]
    assert new_code != code
    r = await bidder_ext.register_bidder_row(stale, "ana", "hash")  # stale hash in the filter no longer matches
    assert r is False
    assert (await ext.post("/ext/register", json=reg(code))).status_code == 400
    assert (await ext.post("/ext/register", json=reg(new_code))).status_code == 200


async def test_login_wrong_password_and_unknown_user_look_the_same(ext):
    await register(ext)
    wrong_pw = await ext.post("/ext/login", json={"username": "ana", "password": "nope-nope"})
    no_user = await ext.post("/ext/login", json={"username": "ghost", "password": "nope-nope"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json() == {"detail": BAD}


async def test_archived_bidder_cannot_log_in(ext):
    bid, _, _ = await register(ext)
    assert (await ext.delete(f"/bidders/{bid}")).status_code == 204
    r = await ext.post("/ext/login", json={"username": "ana", "password": "password1"})
    assert r.status_code == 401 and r.json()["detail"] == BAD


async def test_archive_revokes_tokens(ext):
    bid, _, body = await register(ext)
    await ext.delete(f"/bidders/{bid}")
    assert (await ext.get("/ext/me", headers=auth(body["token"]))).status_code == 401


async def test_reset_login_revokes_tokens(ext):
    bid, _, body = await register(ext)
    reset = await ext.post(f"/bidders/{bid}/reset-login")
    assert reset.status_code == 200
    assert (await ext.get("/ext/me", headers=auth(body["token"]))).status_code == 401
    assert (await ext.post("/ext/login", json={"username": "ana", "password": "password1"})).status_code == 401
    r = await ext.post("/ext/register", json=reg(reset.json()["code"], "ana2"))
    assert r.status_code == 200 and r.json()["bidder"]["folder"] == "ana-lima"


async def test_me_requires_a_valid_bearer(ext):
    assert (await ext.get("/ext/me")).status_code == 401
    assert (await ext.get("/ext/me", headers=auth("garbage"))).status_code == 401


async def test_tokens_do_not_cross(ext, owner):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.routers import bidders
    from app.security import create_access_token

    _, _, body = await register(ext)
    plain = FastAPI()
    plain.include_router(bidders.router)  # real get_current_user
    async with AsyncClient(transport=ASGITransport(app=plain), base_url="http://test") as c:
        r = await c.get("/bidders", headers=auth(body["token"]))
        assert r.status_code == 401 and r.json()["detail"] == "invalid token"
    app_token = create_access_token(str(owner.id), "staff")
    assert (await ext.get("/ext/me", headers=auth(app_token))).status_code == 401


async def test_eleventh_failed_login_is_429(ext):
    await register(ext)
    for _ in range(10):
        r = await ext.post("/ext/login", json={"username": "ana", "password": "wrong-pass"})
        assert r.status_code == 401
    r = await ext.post("/ext/login", json={"username": "ana", "password": "password1"})
    assert r.status_code == 429
    assert r.json()["detail"] == "Too many attempts — try again later"
    assert int(r.headers["retry-after"]) > 0
