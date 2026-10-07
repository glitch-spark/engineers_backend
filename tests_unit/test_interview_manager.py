"""The interview manager role and typed Slack member IDs, against an in-memory MongoDB (mongomock-motor)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import UserExtra

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["manager-unit"]
    await init_beanie(database=database, document_models=[UserExtra])
    return database


async def make_user(name, role="staff", active=True, slack=None) -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, isActive=active, slackUserId=slack).insert()


@pytest.fixture
def client(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import slack_bot, users

    def make(user):
        app = FastAPI()
        app.include_router(users.router)
        app.include_router(slack_bot.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


# --- interview manager role ------------------------------------------------------------------------


async def test_admin_sets_interview_manager_role(client):
    admin, maria = await make_user("Root", "admin"), await make_user("Maria")
    async with client(admin) as c:
        r = await c.put(f"/users/{maria.id}", json={"role": "interview_manager"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "interview_manager"
    assert (await UserExtra.get(maria.id)).role == "interview_manager"


@pytest.mark.parametrize("bad", ["accountant", "owner", ""])
async def test_unknown_role_is_rejected(client, bad):
    admin, maria = await make_user("Root", "admin"), await make_user("Maria")
    async with client(admin) as c:
        assert (await c.put(f"/users/{maria.id}", json={"role": bad})).status_code == 422
        assert (await c.post("/users", json={"name": "New", "email": "n@x.io", "role": bad})).status_code == 422
    assert (await UserExtra.get(maria.id)).role == "staff"


async def test_staff_cannot_change_roles(client):
    pedro = await make_user("Pedro")
    async with client(pedro) as c:
        r = await c.put(f"/users/{pedro.id}", json={"role": "interview_manager"})
    assert r.status_code == 403


async def test_per_user_manager_field_is_gone(client):
    admin, pedro, maria = await make_user("Root", "admin"), await make_user("Pedro"), await make_user("Maria")
    async with client(admin) as c:
        r = await c.put(f"/users/{pedro.id}", json={"interviewManagerId": str(maria.id)})
        assert r.status_code == 200 and "interviewManagerId" not in r.json()
        rows = (await c.get("/users", params={"limit": 50})).json()["users"]
    assert all("interviewManagerId" not in u and "interviewManagerName" not in u for u in rows)


async def test_interview_managers_are_active_role_holders(db):
    from app.slack_bot import interview_managers

    await make_user("Zed", "interview_manager")
    await make_user("Amy", "interview_manager")
    await make_user("Gone", "interview_manager", active=False)
    await make_user("Pedro")
    await make_user("Root", "admin")
    assert [u.name for u in await interview_managers()] == ["Amy", "Zed"]


async def test_no_interview_managers(db):
    from app.slack_bot import interview_managers

    await make_user("Pedro")
    assert await interview_managers() == []


async def test_interview_manager_can_view_any_interview():
    from types import SimpleNamespace

    from app.routers.interviews import _can_view

    iv = SimpleNamespace(createdBy=PydanticObjectId(), caller=None, stageHistory=[])
    assert _can_view(iv, SimpleNamespace(id=PydanticObjectId(), role="interview_manager")) is True
    assert _can_view(iv, SimpleNamespace(id=PydanticObjectId(), role="staff")) is False


# --- typed Slack member ID ------------------------------------------------------------------------


async def test_slack_id_is_normalized_and_saved(client):
    pedro = await make_user("Pedro")
    async with client(pedro) as c:
        r = await c.patch("/integrations/slack/prefs", json={"slackUserId": " u04abc12345 "})
        assert r.status_code == 200, r.text
        assert r.json()["slackUserId"] == "U04ABC12345"
        assert (await c.get("/integrations/slack/status")).json()["slackUserId"] == "U04ABC12345"
    assert (await UserExtra.get(pedro.id)).slackUserId == "U04ABC12345"


@pytest.mark.parametrize("bad", ["@pedro", "U12", "X04ABC12345", "U04ABC12345678", "pedro.sato"])
async def test_slack_id_rejects_bad_format(client, bad):
    pedro = await make_user("Pedro")
    async with client(pedro) as c:
        r = await c.patch("/integrations/slack/prefs", json={"slackUserId": bad})
    assert r.status_code == 400


async def test_slack_id_taken_is_409_and_other_keeps_it(client):
    await make_user("Maria", slack="U04ABC12345")
    pedro = await make_user("Pedro")
    async with client(pedro) as c:
        r = await c.patch("/integrations/slack/prefs", json={"slackUserId": "U04ABC12345"})
    assert (r.status_code, r.json()["detail"]) == (409, "That Slack ID is already used by another user")
    assert (await UserExtra.find_one({"name": "Maria"})).slackUserId == "U04ABC12345"
    assert (await UserExtra.get(pedro.id)).slackUserId is None


async def test_own_slack_id_can_be_saved_again(client):
    pedro = await make_user("Pedro", slack="U04ABC12345")
    async with client(pedro) as c:
        r = await c.patch("/integrations/slack/prefs", json={"slackUserId": "U04ABC12345"})
    assert r.status_code == 200


async def test_blank_slack_id_clears(client):
    pedro = await make_user("Pedro", slack="U04ABC12345")
    async with client(pedro) as c:
        r = await c.patch("/integrations/slack/prefs", json={"slackUserId": "  "})
    assert r.status_code == 200 and r.json()["slackUserId"] is None
    assert (await UserExtra.get(pedro.id)).slackUserId is None


async def test_prefs_without_slack_id_leave_it(client):
    pedro = await make_user("Pedro", slack="U04ABC12345")
    async with client(pedro) as c:
        await c.patch("/integrations/slack/prefs", json={"slackAlertsEnabled": True})
    assert (await UserExtra.get(pedro.id)).slackUserId == "U04ABC12345"


async def test_oauth_routes_are_gone(client):
    pedro = await make_user("Pedro")
    async with client(pedro) as c:
        assert (await c.post("/integrations/slack/oauth-start")).status_code in (404, 405)
        assert (await c.get("/integrations/slack/oauth-callback")).status_code in (404, 405)
        assert (await c.delete("/integrations/slack/disconnect")).status_code in (404, 405)


async def test_lookup_returns_is_active(client):
    pedro = await make_user("Pedro")
    await make_user("Gone", active=False)
    async with client(pedro) as c:
        rows = {u["name"]: u for u in (await c.get("/users/lookup")).json()["users"]}
    assert (rows["Pedro"]["isActive"], rows["Gone"]["isActive"]) == (True, False)
