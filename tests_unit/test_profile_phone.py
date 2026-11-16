"""Users set their own phone through PUT /profile (in-memory MongoDB via mongomock-motor)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import init_beanie
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app.deps import get_current_user
from app.models import UserExtra
from app.routers import profile

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["profile-phone-unit"]
    await init_beanie(database=database, document_models=[UserExtra])
    return database


def _client(user):
    app = FastAPI()
    app.include_router(profile.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_user_can_set_own_phone(db):
    ana = await UserExtra(name="Ana", email="ana@x.io", role="staff").insert()
    base = {"username": "Ana", "email": ana.email}
    async with _client(ana) as c:
        first = (await c.get("/profile")).json()["user"]
        assert first["phone"] == "" and first["isActive"] is True

        res = await c.put("/profile", json={**base, "phone": " +1 555 0100 "})
        assert res.status_code == 200, res.text
        assert res.json()["user"]["phone"] == "+1 555 0100"
        assert (await UserExtra.get(ana.id)).phone == "+1 555 0100"
        assert (await c.get("/profile")).json()["user"]["phone"] == "+1 555 0100"

        # omitting it leaves it unchanged
        assert (await c.put("/profile", json=base)).status_code == 200
        assert (await UserExtra.get(ana.id)).phone == "+1 555 0100"

        # sending an empty string clears it
        assert (await c.put("/profile", json={**base, "phone": "  "})).status_code == 200
        assert not (await UserExtra.get(ana.id)).phone
        assert (await c.get("/profile")).json()["user"]["phone"] == ""
