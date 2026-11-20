"""GET /dashboard/net-monthly: `userId=all` (admins) sums everyone's approved transactions (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Transaction, UserExtra
from app.routers.dashboard import dashboard_net_monthly

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["net-monthly-all"], document_models=[UserExtra, Transaction])


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def _tx(user, amount, status="approved"):
    await Transaction(userId=user.id, date=datetime(2026, 11, 5), amount=amount, status=status).insert()


def _nov(out):
    return next(m for m in out["months"] if m["period"] == "2026-11")


async def test_admin_all_sums_everyone(db):
    admin, ana, ben = await _user("Boss", "admin"), await _user("Ana"), await _user("Ben")
    await _tx(ana, 1000)
    await _tx(ben, -300)
    await _tx(ben, 500, status="pending")
    out = await dashboard_net_monthly(admin, userId="all", today="2026-11-18")
    assert _nov(out)["net"] == 700 and out["total"] == 700


async def test_staff_cannot_ask_for_all(db):
    ana = await _user("Ana")
    with pytest.raises(HTTPException) as e:
        await dashboard_net_monthly(ana, userId="all", today="2026-11-18")
    assert e.value.status_code == 403


async def test_self_unchanged(db):
    ana, ben = await _user("Ana"), await _user("Ben")
    await _tx(ana, 1000)
    await _tx(ben, 400)
    out = await dashboard_net_monthly(ana, userId=None, today="2026-11-18")
    assert _nov(out)["net"] == 1000
