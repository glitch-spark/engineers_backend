"""AUTH_SECRET strength, user-record leaks, staff login hardening and transaction self-approval (in-memory MongoDB)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")

from datetime import datetime
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient
from pydantic import ValidationError

from app.config import Settings
from app.models import Transaction, UserExtra
from app.security import hash_password

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

PASSWORD = "password1"


# --- AUTH_SECRET -------------------------------------------------------------------------------------------


def test_short_auth_secret_refuses_to_boot():
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, MONGODB_URI="m", AUTH_SECRET="x" * 31)
    assert "secrets.token_urlsafe(48)" in str(exc.value)


def test_32_char_auth_secret_is_accepted():
    assert Settings(_env_file=None, MONGODB_URI="m", AUTH_SECRET="x" * 32).AUTH_SECRET == "x" * 32


# --- shared fixtures --------------------------------------------------------------------------------------


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["security-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Transaction])
    return database


async def _user(name, role="staff", **extra) -> UserExtra:
    return await UserExtra(name=name, email=f"{name}@x.io", role=role, **extra).insert()


# --- user record leaks ------------------------------------------------------------------------------------


def test_doc_to_dict_drops_secrets_even_if_present():
    from app.routers._util import doc_to_dict

    doc = SimpleNamespace(model_dump=lambda **_: {
        "id": "1", "name": "a", "hashedPassword": "h", "freeLlmApiKey": "nvapi-x",
    })
    assert doc_to_dict(doc) == {"_id": "1", "name": "a"}


async def test_populate_owner_embeds_only_public_fields(db):
    from app.routers._util import populate_owner

    owner = await _user("ana", phone="555", hashedPassword=hash_password(PASSWORD), resumePromptBody="secret prompt",
                        slackUserId="U1", image="https://img/a.png")
    tx = await Transaction(userId=owner.id, date=datetime(2026, 1, 1), amount=-1).insert()

    [item] = await populate_owner([tx], field="userId", extra=(("payerId", "payer"),))
    assert set(item["userId"]) == {"_id", "name", "email", "image"}
    assert item["ownerEmail"] == "ana@x.io" and item["ownerImage"] == "https://img/a.png"

    [item] = await populate_owner([tx], field="userId", only=("phone",))
    assert set(item["userId"]) == {"_id", "name", "email", "image", "phone"}


async def test_admin_user_list_never_returns_password_hash(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import users

    admin = await _user("boss", role="admin", hashedPassword=hash_password(PASSWORD))
    await _user("ana", phone="555", hashedPassword=hash_password(PASSWORD))
    app = FastAPI()
    app.include_router(users.router)
    app.dependency_overrides[get_current_user] = lambda: admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/users", params={"limit": 50})
        created = await client.post("/users", json={"name": "new", "email": "new@x.io"})
    assert r.status_code == 200
    rows = r.json()["users"]
    assert {u["name"] for u in rows} == {"boss", "ana"}
    for u in rows + [created.json()]:
        assert "hashedPassword" not in u and "freeLlmApiKey" not in u
    assert next(u for u in rows if u["name"] == "ana")["phone"] == "555"  # admins still see contact fields


# --- staff login --------------------------------------------------------------------------------------------


@pytest.fixture
async def auth(db, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.bids.rate_limit import FailureLimiter
    from app.routers import auth as auth_router

    monkeypatch.setattr(auth_router, "LIMITER", FailureLimiter())
    app = FastAPI()
    app.include_router(auth_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _login(auth, username="ana", password="wrong-pass", xff=None):
    return auth.post(
        "/auth/login", json={"username": username, "password": password},
        headers={"X-Forwarded-For": xff} if xff else {},
    )


async def test_login_succeeds_and_failures_are_generic(auth, db):
    await _user("ana", hashedPassword=hash_password(PASSWORD))
    ok = await _login(auth, password=PASSWORD)
    assert ok.status_code == 200, ok.text
    wrong = await _login(auth)
    unknown = await _login(auth, username="nobody")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "invalid credentials"}


async def test_placeholder_hash_is_401_not_500(auth, db, monkeypatch):
    from app.routers import auth as auth_router

    calls = []
    real = auth_router.verify_password
    monkeypatch.setattr(auth_router, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    await _user("ana", hashedPassword="placeholder")  # what POST /users stores
    r = await _login(auth, password="placeholder")
    assert r.status_code == 401
    assert calls == ["placeholder", auth_router._DUMMY_HASH]  # still pays one bcrypt check


async def test_unknown_user_still_runs_bcrypt(auth, db, monkeypatch):
    from app.routers import auth as auth_router

    calls = []
    real = auth_router.verify_password
    monkeypatch.setattr(auth_router, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    await _login(auth, username="nobody")
    assert calls == [auth_router._DUMMY_HASH]


async def test_login_is_rate_limited_per_username(auth, db):
    await _user("ana", hashedPassword=hash_password(PASSWORD))
    for i in range(10):
        # A fresh spoofed left-most X-Forwarded-For entry each time does not make a fresh IP key.
        assert (await _login(auth, xff=f"6.6.6.{i}, 1.2.3.4")).status_code == 401
    r = await _login(auth, password=PASSWORD)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


async def test_login_is_rate_limited_per_ip(auth, db):
    for i in range(10):
        assert (await _login(auth, username=f"user{i}", xff=f"6.6.6.{i}, 1.2.3.4")).status_code == 401
    assert (await _login(auth, username="fresh", xff="9.9.9.9, 1.2.3.4")).status_code == 429
    assert (await _login(auth, username="fresh", xff="5.5.5.5")).status_code == 401  # another client IP


async def test_successful_login_clears_the_username_counter(auth, db):
    await _user("ana", hashedPassword=hash_password(PASSWORD))
    for _ in range(9):
        await _login(auth)
    assert (await _login(auth, password=PASSWORD)).status_code == 200
    for _ in range(9):
        assert (await _login(auth, xff="7.7.7.7")).status_code == 401


@pytest.mark.parametrize("password,detail", [
    ("short", "Password must be at least 8 characters"),
    ("é" * 40, "Password is too long"),  # 80 bytes: bcrypt would silently ignore the tail
], ids=["too-short", "too-long"])
async def test_register_enforces_password_rules(auth, db, password, detail):
    r = await auth.post("/auth/register", json={"username": "ana", "email": "ana@x.io", "password": password})
    assert r.status_code == 400
    assert r.json()["detail"] == detail
    assert await UserExtra.find_one(UserExtra.email == "ana@x.io") is None


async def test_register_accepts_a_valid_password(auth, db):
    r = await auth.post("/auth/register", json={"username": "ana", "email": "ana@x.io", "password": PASSWORD})
    assert r.status_code == 201, r.text


# --- transaction approval ------------------------------------------------------------------------------------


@pytest.fixture
async def txapi(db, monkeypatch):
    """Transactions router with Slack and recurring-charge side effects stubbed. `txapi.as_(user)` switches caller."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app import slack_transactions
    from app.deps import get_current_user
    from app.routers import transactions

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(transactions, "upsert_charge_from_transaction", noop)
    monkeypatch.setattr(transactions, "sync_transaction_post", noop)
    monkeypatch.setattr(slack_transactions, "announce_decision", noop)
    current = {}
    app = FastAPI()
    app.include_router(transactions.router)
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.as_ = lambda user: current.__setitem__("user", user)
        yield client


async def _create(api, owner, payer):
    api.as_(owner)
    r = await api.post("/transactions", json={
        "date": "2026-03-10", "amount": -4, "payMethod": "coin", "payerId": str(payer.id),
    })
    assert r.status_code == 201, r.text
    return r.json()["_id"]


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_staff_cannot_decide_own_self_paid_transaction(txapi, db, decision):
    ana = await _user("ana")
    tx_id = await _create(txapi, ana, ana)
    r = await txapi.put(f"/transactions/{tx_id}", json={"status": decision})
    assert r.status_code == 403
    assert (await Transaction.find_one({})).status == "pending"


async def test_owner_switching_payer_to_self_cannot_approve(txapi, db):
    ana, bob = await _user("ana"), await _user("bob")
    tx_id = await _create(txapi, ana, bob)
    assert (await txapi.put(f"/transactions/{tx_id}", json={"payerId": str(ana.id)})).status_code == 200
    r = await txapi.put(f"/transactions/{tx_id}", json={"status": "approved"})
    assert r.status_code == 403
    assert (await Transaction.find_one({})).status == "pending"


async def test_payer_other_than_owner_can_approve(txapi, db):
    ana, bob = await _user("ana"), await _user("bob")
    tx_id = await _create(txapi, ana, bob)
    txapi.as_(bob)
    r = await txapi.put(f"/transactions/{tx_id}", json={"status": "approved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["approvedBy"] == str(bob.id)


async def test_admin_can_approve_self_paid_transactions(txapi, db):
    ana, boss = await _user("ana"), await _user("boss", role="admin")
    staff_tx = await _create(txapi, ana, ana)
    admin_tx = await _create(txapi, boss, boss)
    txapi.as_(boss)
    for tx_id in (staff_tx, admin_tx):
        r = await txapi.put(f"/transactions/{tx_id}", json={"status": "approved"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"


async def test_listed_owner_and_payer_objects_are_public_fields_only(txapi, db):
    ana = await _user("ana", phone="555", hashedPassword=hash_password(PASSWORD))
    bob = await _user("bob", phone="777")
    await _create(txapi, ana, bob)
    r = await txapi.get("/transactions")
    assert r.status_code == 200, r.text
    [row] = r.json()["transactions"]
    assert set(row["userId"]) == set(row["payerId"]) == {"_id", "name", "email", "image"}
    assert row["payerName"] == "bob"
