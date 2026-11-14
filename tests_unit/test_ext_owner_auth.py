"""Owners in the Bid Track extension (spec 2026-11-15-self-bids-extension-design.md): sign in with the platform login
and one of their profiles; the token is bound to that profile and kept apart from app and bidder tokens
(mongomock-motor, ASGI client)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.security import create_bidder_token, hash_password

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

PW = "Correct-horse-9"


@pytest.fixture
async def api():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.bids import rate_limit
    from app.routers import bidder_ext, bidders, owner_ext

    await init_beanie(database=AsyncMongoMockClient()["ext-owner-auth-unit"], document_models=[
        UserExtra, Account, Bidder, Bid])
    rate_limit.LIMITER.clear_all() if hasattr(rate_limit.LIMITER, "clear_all") else None
    app = FastAPI()
    for r in (owner_ext.router, bidder_ext.router, bidders.router):
        app.include_router(r)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def owner(name="Pedro", role="staff", active=True):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, isActive=active,
                           hashedPassword=hash_password(PW)).insert()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def login(api, username, profile_id, password=PW):
    return await api.post("/ext/owner/login", json={"username": username, "password": password, "profileId": profile_id})


async def test_profiles_lists_only_the_owners_active_profiles(api):
    pedro, maria = await owner("Pedro"), await owner("Maria")
    a = await Account(name="Alpha", createdBy=pedro.id).insert()
    await Account(name="Old", createdBy=pedro.id, archived=True).insert()
    await Account(name="Hers", createdBy=maria.id).insert()
    r = await api.post("/ext/owner/profiles", json={"username": "pedro@x.io", "password": PW})
    assert r.status_code == 200 and r.json() == {"profiles": [{"id": str(a.id), "name": "Alpha"}]}
    assert (await api.post("/ext/owner/profiles", json={"username": "Pedro", "password": "nope"})).status_code == 401


async def test_admins_and_inactive_users_cannot_sign_in(api):
    await owner("Root", role="admin")
    await owner("Wait", active=False)
    for name in ("Root", "Wait"):
        assert (await api.post("/ext/owner/profiles", json={"username": name, "password": PW})).status_code == 403


async def test_login_binds_the_token_to_one_of_the_owners_profiles(api):
    pedro, maria = await owner("Pedro"), await owner("Maria")
    a = await Account(name="Alpha", createdBy=pedro.id).insert()
    hers = await Account(name="Hers", createdBy=maria.id).insert()
    old = await Account(name="Old", createdBy=pedro.id, archived=True).insert()
    for pid in (str(hers.id), str(old.id), "not-an-id"):
        assert (await login(api, "Pedro", pid)).status_code == 403
    r = await login(api, "Pedro", str(a.id))
    assert r.status_code == 200
    body = r.json()
    assert body["owner"] == {"id": str(pedro.id), "name": "Pedro", "profile": {"id": str(a.id), "name": "Alpha"}}
    me = await api.get("/ext/owner/me", headers=bearer(body["token"]))
    assert me.status_code == 200 and me.json()["profile"]["name"] == "Alpha"


async def test_tokens_stay_apart(api):
    pedro = await owner("Pedro")
    a = await Account(name="Alpha", createdBy=pedro.id).insert()
    token = (await login(api, "Pedro", str(a.id))).json()["token"]
    assert (await api.get("/ext/me", headers=bearer(token))).status_code == 401          # bidder route
    assert (await api.get("/bidders", headers=bearer(token))).status_code == 401         # app route
    b = await Bidder(createdBy=pedro.id, name="Ana", rate=0.5, screenshotFolderUrl="s3://x/", folder="ana",
                     registeredAt=pedro.createdAt or a.createdAt).insert()
    bidder_token = create_bidder_token(str(b.id), b.tokenVersion)
    assert (await api.get("/ext/owner/me", headers=bearer(bidder_token))).status_code == 401


async def test_archiving_the_profile_ends_its_sign_in(api):
    pedro = await owner("Pedro")
    a = await Account(name="Alpha", createdBy=pedro.id).insert()
    token = (await login(api, "Pedro", str(a.id))).json()["token"]
    a.archived = True
    await a.save()
    assert (await api.get("/ext/owner/me", headers=bearer(token))).status_code == 401
