"""Extension routes (/ext) against an in-memory MongoDB; real bidder tokens, no auth override on /ext."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bid_report import BidReport
from app.models.bidder import Bidder
from app.models.slack_alert_log import SlackAlertLog

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

BAD = "Wrong username or password"
INVALID = "Invite code is invalid or expired"
USERNAME_RULE = "Username must be 3–40 characters: lowercase letters, numbers, dots, underscores or dashes"
SHORT_PASSWORD = "Password must be at least 8 characters"
LONG_PASSWORD = "Password is too long"


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["ext-unit"]
    await init_beanie(database=database, document_models=[Bidder, Bid, BidReport, Account, UserExtra, SlackAlertLog])
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
    from app.bids.storage import reset_bid_storage
    from app.config import get_settings
    from app.deps import get_current_user
    from app.routers import bidder_ext, bidders

    app = FastAPI()
    app.include_router(bidders.router)
    app.include_router(bidder_ext.router)
    app.dependency_overrides[get_current_user] = lambda: owner
    monkeypatch.setenv("BID_STORAGE_BUCKET", "bid-screenshots")
    get_settings.cache_clear()
    reset_bid_storage()
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
    for body, message in (
        (reg(code, "ab"), USERNAME_RULE),
        (reg(code, "Bad Name!"), USERNAME_RULE),
        (reg(code, "x" * 41), USERNAME_RULE),
        (reg(code, "ana", "1234567"), SHORT_PASSWORD),
        (reg(code, "ana", "x" * 73), LONG_PASSWORD),
        (reg(code, "ana", "é" * 37), LONG_PASSWORD),  # 37 characters, 74 bytes
    ):
        r = await ext.post("/ext/register", json=body)
        assert (r.status_code, r.json()["detail"]) == (400, message), body
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


async def test_register_treats_stored_nulls_as_unset_but_still_refuses_archived(ext):
    from app.routers import bidder_ext

    bid, code = await ext.create()
    coll, oid = Bidder.get_motor_collection(), PydanticObjectId(bid)
    await coll.update_one({"_id": oid}, {"$set": {"registeredAt": None, "archivedAt": None}})  # explicit nulls
    stale = await Bidder.get(oid)
    await coll.update_one({"_id": oid}, {"$set": {"archivedAt": datetime.now(timezone.utc)}})
    assert await bidder_ext.register_bidder_row(stale, "ana", "hash") is False
    await coll.update_one({"_id": oid}, {"$set": {"archivedAt": None}})
    r = await ext.post("/ext/register", json=reg(code))
    assert r.status_code == 200, r.text


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


def _token_issued(bidder_id, version, issued_at=None):
    """A bidder token issued at `issued_at`; None makes one from before tokens carried `iat`."""
    from jose import jwt

    from app.config import get_settings

    claims = {"sub": bidder_id, "typ": "bidder", "ver": version, "exp": datetime.now(timezone.utc) + timedelta(days=20)}
    if issued_at is not None:
        claims["iat"] = issued_at
    return jwt.encode(claims, get_settings().AUTH_SECRET, algorithm="HS256")


def _claims(token):
    from app.security import decode_token

    return decode_token(token)


def _days_ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


async def test_a_week_old_token_is_renewed(ext, storage):
    bid, _, body = await register(ext)
    assert "iat" in _claims(body["token"])
    old = _token_issued(bid, 0, _days_ago(8))
    me = (await ext.get("/ext/me", headers=auth(old))).json()
    renewed = me.pop("token")
    assert me == body["bidder"]
    claims = _claims(renewed)
    assert (claims["sub"], claims["typ"], claims["ver"]) == (bid, "bidder", 0)
    assert claims["exp"] - datetime.now(timezone.utc).timestamp() > 29 * 86400
    again = await ext.get("/ext/me", headers=auth(renewed))
    assert again.status_code == 200 and "token" not in again.json()
    up = await ext.post("/ext/upload-url", json=_shot()[0], headers=auth(old))
    assert up.status_code == 200 and _claims(up.json()["token"])["ver"] == 0
    up = await ext.post("/ext/upload-url", json=_shot()[0], headers=auth(renewed))
    assert up.status_code == 200 and "token" not in up.json()


async def test_fresh_tokens_are_not_renewed_and_tokens_without_iat_are(ext):
    bid, _, body = await register(ext)
    assert "token" not in (await ext.get("/ext/me", headers=auth(body["token"]))).json()
    assert "token" not in (await ext.get("/ext/me", headers=auth(_token_issued(bid, 0, _days_ago(6))))).json()
    assert "token" in (await ext.get("/ext/me", headers=auth(_token_issued(bid, 0)))).json()


async def test_renewal_keeps_the_version_and_never_revives_a_reset_token(ext):
    bid, _, _ = await register(ext)
    b = await Bidder.get(PydanticObjectId(bid))
    b.tokenVersion = 3
    await b.save()
    r = await ext.get("/ext/me", headers=auth(_token_issued(bid, 3, _days_ago(8))))
    assert _claims(r.json()["token"])["ver"] == 3
    assert (await ext.get("/ext/me", headers=auth(_token_issued(bid, 2, _days_ago(8))))).status_code == 401


async def test_eleventh_failed_login_is_429(ext):
    await register(ext)
    for _ in range(10):
        r = await ext.post("/ext/login", json={"username": "ana", "password": "wrong-pass"})
        assert r.status_code == 401
    r = await ext.post("/ext/login", json={"username": "ana", "password": "password1"})
    assert r.status_code == 429
    assert r.json()["detail"] == "Too many attempts — try again later"
    assert int(r.headers["retry-after"]) > 0


async def _login(ext, user="ana", pw="wrong-pass", ip=None):
    return await ext.post(
        "/ext/login", json={"username": user, "password": pw}, headers={"X-Forwarded-For": ip} if ip else {}
    )


async def test_concurrent_failed_logins_are_capped(ext, monkeypatch):
    import asyncio

    await register(ext)
    real = Bidder.find_one.__func__

    async def slow_find_one(cls, *a, **kw):
        await asyncio.sleep(0)  # real Motor yields here
        return await real(cls, *a, **kw)

    monkeypatch.setattr(Bidder, "find_one", classmethod(slow_find_one))
    codes = [r.status_code for r in await asyncio.gather(*[_login(ext) for _ in range(25)])]
    assert codes.count(401) <= 10 and codes.count(429) >= 15


async def test_username_case_shares_one_counter(ext):
    await register(ext)
    for user in ["ANA", "ana"] * 5:
        assert (await _login(ext, user)).status_code == 401
    assert (await _login(ext, "Ana", "password1")).status_code == 429


async def test_ip_limit_spans_usernames(ext):
    for i in range(10):
        assert (await _login(ext, f"user{i}", ip="5.5.5.5")).status_code == 401
    assert (await _login(ext, "someone-new", ip="5.5.5.5")).status_code == 429
    assert (await _login(ext, "someone-new", ip="6.6.6.6")).status_code == 401


async def test_failed_registrations_count(ext):
    for _ in range(10):
        r = await ext.post("/ext/register", json=reg("ZZZZ-AAAA-BBBB"))
        assert r.status_code == 400
    assert (await ext.post("/ext/register", json=reg("ZZZZ-AAAA-BBBB"))).status_code == 429


async def test_bcrypt_runs_off_the_event_loop(ext, monkeypatch):
    import asyncio

    ran, real = [], asyncio.to_thread

    async def recording(fn, *a, **kw):
        ran.append(fn.__name__)
        return await real(fn, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", recording)
    await register(ext)
    assert (await _login(ext, "ana", "password1")).status_code == 200
    assert (await _login(ext, "ghost", "password1")).status_code == 401  # the dummy check
    assert (ran.count("hash_password"), ran.count("verify_password")) == (1, 2)


async def test_long_inputs_make_capped_limiter_keys(ext):
    from app.routers import bidder_ext

    forged = "f" * 500
    assert (await _login(ext, "ana", ip=forged)).status_code == 401
    assert set(bidder_ext.LIMITER._failures) == {"user:ana", "ip:" + "f" * 64}


async def test_login_explains_the_username_and_password_rules(ext):
    for body, message in (
        ({"username": "ana@example.com", "password": "password1"}, USERNAME_RULE),
        ({"username": "ana", "password": "short"}, SHORT_PASSWORD),
    ):
        r = await ext.post("/ext/login", json=body)
        assert (r.status_code, r.json()["detail"]) == (400, message), body


async def test_rule_failures_count_toward_the_limit(ext):
    for _ in range(10):
        assert (await ext.post("/ext/register", json=reg("ZZZZ-AAAA-BBBB", "a b"))).status_code == 400
    assert (await ext.post("/ext/register", json=reg("ZZZZ-AAAA-BBBB", "a b"))).status_code == 429


@pytest.fixture
async def real_app(db, monkeypatch):
    """The production app (create_app, with its error handlers) without running its lifespan (no real DB)."""
    from httpx import ASGITransport, AsyncClient

    from app.bids.rate_limit import FailureLimiter
    from app.main import create_app
    from app.routers import bidder_ext

    monkeypatch.setattr(bidder_ext, "LIMITER", FailureLimiter())
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        yield client


async def test_production_error_bodies_carry_a_readable_error(real_app):
    r = await real_app.post("/ext/register", json=reg("ZZZZ-AAAA-BBBB"))
    assert (r.status_code, r.json()) == (400, {"error": INVALID})
    r = await real_app.post("/ext/register", json=reg("ZZZZ-AAAA-BBBB", "Bad Name!"))
    assert (r.status_code, r.json()) == (400, {"error": USERNAME_RULE})
    r = await real_app.post("/ext/login", json={"username": "x" * 101, "password": "password1"})
    assert (r.status_code, r.json()) == (400, {"error": "Invalid request"})  # past the schema's length cap


async def test_successful_logins_do_not_count_toward_the_ip(ext):
    await register(ext)
    for _ in range(12):
        assert (await _login(ext, "ana", "password1", ip="7.7.7.7")).status_code == 200


class _FakeStorage:
    def __init__(self, error=None):
        self.calls = []
        self.content_types = []
        self.error = error

    def presign_put(self, bucket, key, content_type="image/png"):
        if self.error:
            raise self.error
        self.calls.append((bucket, key))
        self.content_types.append(content_type)
        return f"https://signed/{bucket}/{key}"


def _shot(**over):
    at = datetime.now(timezone.utc).replace(microsecond=0)
    return {"job": "acme_1", "step": 1, "trigger": "step", "capturedAt": at.isoformat(), **over}, at


@pytest.fixture
def storage(monkeypatch):
    from app.routers import bidder_ext

    fake = _FakeStorage()
    monkeypatch.setattr(bidder_ext, "get_bid_storage", lambda: fake)
    return fake


async def _profile(owner, name):
    return await Account(name=name, createdBy=owner.id).insert()


async def test_upload_url_signs_the_bidders_path(ext, owner, storage):
    bid, _, body = await register(ext)
    account = await _profile(owner, "Upwork")
    b = await Bidder.get(PydanticObjectId(bid))
    b.profileId = account.id
    await b.save()
    payload, at = _shot()
    r = await ext.post("/ext/upload-url", json=payload, headers=auth(body["token"]))
    assert r.status_code == 200, r.text
    key = f"ana-lima/{at:%Y-%m-%d}/upwork/acme_1/step1_step_{at:%Y-%m-%d}_{at:%H%M%S}.png"
    assert r.json()["key"] == key
    assert r.json()["url"] == f"https://signed/bid-screenshots/{key}"
    assert r.json()["headers"] == {"Content-Type": "image/png"}
    assert datetime.fromisoformat(r.json()["expiresAt"]) > datetime.now(timezone.utc)
    assert storage.calls == [("bid-screenshots", key)]


async def test_upload_uses_the_current_profile(ext, owner, storage):
    bid, _, body = await register(ext)
    first, second = await _profile(owner, "Upwork"), await _profile(owner, "Backend")
    for account, segment in ((first, "/upwork/"), (second, "/backend/")):
        r = await ext.put(f"/bidders/{bid}", json={"name": "Ana Lima", "rate": 0.5, "profileId": str(account.id)})
        assert r.status_code == 200, r.text
        r = await ext.post("/ext/upload-url", json=_shot()[0], headers=auth(body["token"]))
        assert segment in r.json()["key"]


async def test_upload_url_records_the_bid(ext, storage):
    _, _, body = await register(ext)
    payload, at = _shot(jobUrl="https://www.upwork.com/jobs/~01", jobTitle="Fix my roof")
    r = await ext.post("/ext/upload-url", json=payload, headers=auth(body["token"]))
    assert r.status_code == 200, r.text
    bids = await Bid.find_all().to_list()
    assert len(bids) == 1 and r.json()["bidId"] == str(bids[0].id)
    assert (bids[0].jobUrl, bids[0].jobTitle) == ("https://www.upwork.com/jobs/~01", "Fix my roof")
    assert [(s.key, s.step, s.trigger, s.uploadedAt) for s in bids[0].screenshots] == [
        (r.json()["key"], 1, "step", None)
    ]


def _resume(**over):
    return {
        "field": "Resume/CV", "name": "Ana_Lima.pdf", "size": 48213, "type": "application/pdf",
        "lastModified": "2026-10-01T09:30:00.000Z", "path": "C:\\Users\\ana\\Resumes\\Ana_Lima.pdf",
        "inField": True, **over,
    }


async def test_upload_url_records_the_attached_files(ext, storage):
    _, _, body = await register(ext)
    unread = _resume(field=None, name="cover.pdf", path=None, pathError="Not allowed", inField=False)
    r = await ext.post("/ext/upload-url", json=_shot(attachments=[_resume(), unread])[0], headers=auth(body["token"]))
    assert r.status_code == 200, r.text
    [first, second] = (await Bid.find_one()).screenshots[0].attachments
    assert (first.field, first.name, first.size, first.type, first.path, first.pathError, first.inField) == (
        "Resume/CV", "Ana_Lima.pdf", 48213, "application/pdf", "C:\\Users\\ana\\Resumes\\Ana_Lima.pdf", None, True,
    )
    assert first.lastModified == datetime(2026, 10, 1, 9, 30)  # naive UTC like the other stored times
    assert (second.name, second.path, second.pathError, second.inField) == ("cover.pdf", None, "Not allowed", False)


async def test_upload_url_cuts_attachment_text_instead_of_refusing(ext, storage):
    _, _, body = await register(ext)
    many = [_resume(name=f"{i}.pdf", path="C:\\" + "p" * 1200, field=" " * 3) for i in range(25)]
    r = await ext.post("/ext/upload-url", json=_shot(attachments=many)[0], headers=auth(body["token"]))
    assert r.status_code == 200, r.text
    stored = (await Bid.find_one()).screenshots[0].attachments
    assert len(stored) == 20 and [a.name for a in stored[:2]] == ["0.pdf", "1.pdf"]
    assert len(stored[0].path) == 1000 and stored[0].field is None


async def test_upload_url_without_attachments_stores_none(ext, storage):
    _, _, body = await register(ext)
    r = await ext.post("/ext/upload-url", json=_shot()[0], headers=auth(body["token"]))
    assert r.status_code == 200, r.text
    assert (await Bid.find_one()).screenshots[0].attachments == []
    raw = await Bid.get_motor_collection().find_one()
    assert "attachments" not in raw["screenshots"][0]


async def test_upload_url_truncates_the_job_title(ext, storage):
    _, _, body = await register(ext)
    r = await ext.post("/ext/upload-url", json=_shot(jobTitle="t" * 400)[0], headers=auth(body["token"]))
    assert r.status_code == 200, r.text
    assert (await Bid.find_one()).jobTitle == "t" * 300


async def test_upload_url_drops_a_non_http_job_url(ext, storage):
    _, _, body = await register(ext)
    r = await ext.post("/ext/upload-url", json=_shot(jobUrl="javascript:alert(1)")[0], headers=auth(body["token"]))
    assert r.status_code == 200, r.text
    assert (await Bid.find_one()).jobUrl is None


async def test_upload_url_rejects_a_too_long_job_url(ext, real_app, storage):
    _, _, body = await register(ext)
    h = auth(body["token"])
    too_long = "https://x.io/" + "a" * (2001 - len("https://x.io/"))
    r = await real_app.post("/ext/upload-url", json=_shot(jobUrl=too_long)[0], headers=h)
    assert (r.status_code, r.json()) == (400, {"error": "jobUrl is too long"})
    assert await Bid.find_all().to_list() == [] and storage.calls == []
    r = await real_app.post("/ext/upload-url", json=_shot(jobUrl=too_long[:-1])[0], headers=h)
    assert r.status_code == 200, r.text


async def test_upload_url_without_job_fields_still_works(ext, storage):
    _, _, body = await register(ext)
    r = await ext.post("/ext/upload-url", json=_shot()[0], headers=auth(body["token"]))
    assert r.status_code == 200, r.text
    bid = await Bid.find_one()
    assert str(bid.id) == r.json()["bidId"] and (bid.jobUrl, bid.jobTitle) == (None, None)


async def test_confirm_sets_uploaded_at(ext, storage):
    _, _, body = await register(ext)
    h = auth(body["token"])
    key = (await ext.post("/ext/upload-url", json=_shot()[0], headers=h)).json()["key"]
    r = await ext.post("/ext/uploads/confirm", json={"key": key}, headers=h)
    assert (r.status_code, r.json()) == (200, {"ok": True})
    assert (await Bid.find_one()).screenshots[0].uploadedAt is not None


async def test_confirm_of_an_unknown_key_is_404(ext, storage):
    _, _, body = await register(ext)
    r = await ext.post("/ext/uploads/confirm", json={"key": "ana-lima/nope.png"}, headers=auth(body["token"]))
    assert (r.status_code, r.json()) == (404, {"detail": "upload not found"})


async def test_confirm_requires_a_bidder_token_and_only_reaches_own_uploads(ext, storage):
    _, _, ana = await register(ext)
    key = (await ext.post("/ext/upload-url", json=_shot()[0], headers=auth(ana["token"]))).json()["key"]
    assert (await ext.post("/ext/uploads/confirm", json={"key": key})).status_code == 401
    _, _, bo = await register(ext, "Bob Lima", "bobby")
    r = await ext.post("/ext/uploads/confirm", json={"key": key}, headers=auth(bo["token"]))
    assert r.status_code == 404
    assert (await Bid.find_one()).screenshots[0].uploadedAt is None


async def test_confirm_404_carries_a_readable_error(ext, real_app, storage):
    _, _, body = await register(ext)
    r = await real_app.post("/ext/uploads/confirm", json={"key": "x/y.png"}, headers=auth(body["token"]))
    assert (r.status_code, r.json()) == (404, {"error": "upload not found"})


async def test_upload_url_signs_the_requested_image_format(ext, storage):
    _, _, body = await register(ext)
    h = auth(body["token"])
    for fmt, ext_, mime in (("webp", "webp", "image/webp"), ("jpeg", "jpg", "image/jpeg"), ("png", "png", "image/png")):
        r = await ext.post("/ext/upload-url", json=_shot(format=fmt)[0], headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["key"].endswith(f".{ext_}")
        assert r.json()["headers"] == {"Content-Type": mime}
        assert storage.content_types[-1] == mime


async def test_upload_url_defaults_to_png_for_older_extensions(ext, storage):
    _, _, body = await register(ext)
    r = await ext.post("/ext/upload-url", json=_shot()[0], headers=auth(body["token"]))
    assert r.json()["key"].endswith(".png") and storage.content_types == ["image/png"]


async def test_upload_url_validation(ext, storage):
    _, _, body = await register(ext)
    h = auth(body["token"])
    naive = datetime.now().replace(microsecond=0).isoformat()
    for over in ({"step": 0}, {"step": 501}, {"trigger": "click"}, {"capturedAt": naive}, {"job": ""}, {"format": "gif"}):
        r = await ext.post("/ext/upload-url", json=_shot(**over)[0], headers=h)
        assert r.status_code == 422, over
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    r = await ext.post("/ext/upload-url", json=_shot(capturedAt=old)[0], headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "capturedAt is not current"
    assert storage.calls == []


async def test_upload_url_requires_a_live_bidder_token(ext, storage):
    bid, _, body = await register(ext)
    assert (await ext.post("/ext/upload-url", json=_shot()[0])).status_code == 401
    await ext.delete(f"/bidders/{bid}")
    assert (await ext.post("/ext/upload-url", json=_shot()[0], headers=auth(body["token"]))).status_code == 401


async def test_upload_url_without_storage_is_503(ext, storage, monkeypatch):
    from app.config import get_settings

    _, _, body = await register(ext)
    h = auth(body["token"])
    monkeypatch.delenv("BID_STORAGE_BUCKET")
    get_settings.cache_clear()
    r = await ext.post("/ext/upload-url", json=_shot()[0], headers=h)
    assert r.status_code == 503 and r.json()["detail"] == "Uploads are not configured"


async def test_upload_url_unsupported_provider_or_storage_error_is_503(ext, monkeypatch):
    from app.bids.storage import BidStorageError, _UnsupportedStorage
    from app.routers import bidder_ext

    _, _, body = await register(ext)
    h = auth(body["token"])
    monkeypatch.setattr(bidder_ext, "get_bid_storage", lambda: _UnsupportedStorage("gcs"))
    r = await ext.post("/ext/upload-url", json=_shot()[0], headers=h)
    assert r.status_code == 503 and r.json()["detail"] == "Uploads are not configured"
    monkeypatch.setattr(bidder_ext, "get_bid_storage", lambda: _FakeStorage(BidStorageError("invalid endpoint")))
    r = await ext.post("/ext/upload-url", json=_shot()[0], headers=h)
    assert r.status_code == 503 and r.json()["detail"] == "Uploads are not configured"


async def test_backblaze_without_its_own_keys_is_503_and_never_leaks_aws_keys(ext, monkeypatch):
    from app.config import get_settings

    _, _, body = await register(ext)
    for k, v in {"BID_STORAGE_ENDPOINT_URL": "https://s3.us-east-005.backblazeb2.com",
                 "AWS_ACCESS_KEY_ID": "AKIAAWSLEAK", "AWS_SECRET_ACCESS_KEY": "awssecret",
                 "BID_STORAGE_ACCESS_KEY_ID": "005abc"}.items():  # key id only: half-set
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BID_STORAGE_SECRET_ACCESS_KEY", "")
    get_settings.cache_clear()
    r = await ext.post("/ext/upload-url", json=_shot()[0], headers=auth(body["token"]))
    assert r.status_code == 503 and "AKIAAWSLEAK" not in r.text


async def test_registering_switches_counting_to_the_bidders_folder(ext):
    bid, code = await ext.create("Test Bidder")
    b = await Bidder.get(PydanticObjectId(bid))
    b.screenshotFolderUrl, b.folder = "s3://bucket/test", "test"
    await b.save()
    assert (await ext.post("/ext/register", json=reg(code))).status_code == 200
    assert (await Bidder.get(PydanticObjectId(bid))).screenshotFolderUrl == "s3://bid-screenshots/test/"


async def _change_password(ext, token, current="password1", new="newpass123"):
    return await ext.post("/ext/password", json={"currentPassword": current, "newPassword": new}, headers=auth(token))


async def test_change_password_logs_out_other_sessions(ext):
    _, _, body = await register(ext)
    other = (await _login(ext, pw="password1")).json()["token"]   # the same login in another browser
    r = await _change_password(ext, body["token"])
    assert r.status_code == 200, r.text
    fresh = r.json()["token"]
    assert (await ext.get("/ext/me", headers=auth(fresh))).status_code == 200
    for old in (body["token"], other):
        assert (await ext.get("/ext/me", headers=auth(old))).status_code == 401
    assert (await _login(ext, pw="newpass123")).status_code == 200
    assert (await _login(ext, pw="password1")).status_code == 401


async def test_change_password_needs_the_current_password(ext):
    _, _, body = await register(ext)
    r = await _change_password(ext, body["token"], current="not-my-password")
    assert r.status_code == 400 and r.json()["detail"] == "Current password is wrong"
    assert (await _login(ext, pw="password1")).status_code == 200


async def test_change_password_rules(ext):
    _, _, body = await register(ext)
    r = await _change_password(ext, body["token"], new="short")
    assert r.status_code == 400 and r.json()["detail"] == "Password must be at least 8 characters"
    r = await _change_password(ext, body["token"], new="password1")
    assert r.status_code == 400 and r.json()["detail"] == "Choose a password different from the current one"
    assert (await _login(ext, pw="password1")).status_code == 200


async def test_wrong_current_passwords_hit_the_limit(ext):
    _, _, body = await register(ext)
    for _ in range(10):
        assert (await _change_password(ext, body["token"], current="wrong-pass")).status_code == 400
    assert (await _change_password(ext, body["token"])).status_code == 429


async def test_change_password_requires_a_bidder_token(ext):
    r = await ext.post("/ext/password", json={"currentPassword": "password1", "newPassword": "newpass123"})
    assert r.status_code == 401
