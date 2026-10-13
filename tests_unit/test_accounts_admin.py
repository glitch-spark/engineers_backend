"""Admins can archive, edit and delete any user's profile; staff only their own (mongomock-motor)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import init_beanie
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app import resume_files
from app.deps import get_current_user
from app.models import Account, UserExtra
from app.routers import accounts as accounts_router

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["accounts-admin-unit"]
    await init_beanie(database=database, document_models=[Account, UserExtra])
    deleted: list[str] = []

    async def fake_delete(keys):
        deleted.extend(keys)

    monkeypatch.setattr(resume_files, "delete_resume_files", fake_delete)
    return deleted


async def make_user(name, role="staff") -> UserExtra:
    user = UserExtra(name=name, email=f"{name.lower()}@x.io", hashedPassword="x", role=role)
    await user.insert()
    return user


async def make_account(owner: UserExtra, name="Backend") -> Account:
    acc = Account(name=name, createdBy=owner.id)
    await acc.insert()
    return acc


def client(user) -> AsyncClient:
    app = FastAPI()
    app.include_router(accounts_router.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_admin_archives_and_restores_another_users_profile(db):
    owner, admin = await make_user("Pedro"), await make_user("Root", role="admin")
    acc = await make_account(owner)

    r = await client(admin).put(f"/accounts/{acc.id}", json={"archived": True})
    assert r.status_code == 200, r.text
    stored = await Account.get(acc.id)
    assert stored.archived is True and stored.archivedAt is not None
    assert stored.createdBy == owner.id  # ownership is unchanged

    r = await client(admin).put(f"/accounts/{acc.id}", json={"archived": False})
    assert r.status_code == 200, r.text
    stored = await Account.get(acc.id)
    assert stored.archived is False and stored.archivedAt is None


async def test_admin_deletes_another_users_profile(db):
    owner, admin = await make_user("Pedro"), await make_user("Root", role="admin")
    acc = await make_account(owner)

    r = await client(admin).delete(f"/accounts/{acc.id}")
    assert (r.status_code, r.json()) == (200, {"ok": True})
    assert await Account.get(acc.id) is None


async def test_staff_cannot_touch_another_users_profile(db):
    owner, other = await make_user("Pedro"), await make_user("Maria")
    acc = await make_account(owner)

    assert (await client(other).put(f"/accounts/{acc.id}", json={"archived": True})).status_code == 404
    assert (await client(other).delete(f"/accounts/{acc.id}")).status_code == 404
    stored = await Account.get(acc.id)
    assert stored is not None and stored.archived is False


async def test_owner_still_archives_and_deletes_own_profile(db):
    owner = await make_user("Pedro")
    acc = await make_account(owner)

    assert (await client(owner).put(f"/accounts/{acc.id}", json={"archived": True})).status_code == 200
    assert (await Account.get(acc.id)).archived is True
    assert (await client(owner).delete(f"/accounts/{acc.id}")).status_code == 200
    assert await Account.get(acc.id) is None
