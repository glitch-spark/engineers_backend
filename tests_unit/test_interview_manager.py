"""Interview managers (admin-set) and typed Slack member IDs, against an in-memory MongoDB (mongomock-motor)."""

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


# --- interview manager -----------------------------------------------------------------------------


async def test_admin_sets_and_clears_manager(client):
    admin, pedro, maria = await make_user("Root", "admin"), await make_user("Pedro"), await make_user("Maria")
    async with client(admin) as c:
        r = await c.put(f"/users/{pedro.id}", json={"interviewManagerId": str(maria.id)})
        assert r.status_code == 200, r.text
        assert r.json()["interviewManagerId"] == str(maria.id)
        assert (await UserExtra.get(pedro.id)).interviewManagerId == maria.id
        r = await c.put(f"/users/{pedro.id}", json={"interviewManagerId": None})
        assert r.status_code == 200 and r.json()["interviewManagerId"] is None
    assert (await UserExtra.get(pedro.id)).interviewManagerId is None


async def test_name_only_update_keeps_manager(client):
    admin, pedro, maria = await make_user("Root", "admin"), await make_user("Pedro"), await make_user("Maria")
    async with client(admin) as c:
        await c.put(f"/users/{pedro.id}", json={"interviewManagerId": str(maria.id)})
        await c.put(f"/users/{pedro.id}", json={"name": "Pedro S"})
    assert (await UserExtra.get(pedro.id)).interviewManagerId == maria.id


async def test_manager_must_be_active_other_user(client):
    admin, pedro = await make_user("Root", "admin"), await make_user("Pedro")
    gone = await make_user("Gone", active=False)
    async with client(admin) as c:
        for bad in (str(pedro.id), str(gone.id), str(PydanticObjectId()), "nope"):
            r = await c.put(f"/users/{pedro.id}", json={"interviewManagerId": bad})
            assert (r.status_code, r.json()["detail"]) == (400, "Choose another active user"), bad


async def test_staff_cannot_set_manager(client):
    pedro, maria = await make_user("Pedro"), await make_user("Maria")
    async with client(pedro) as c:
        r = await c.put(f"/users/{pedro.id}", json={"interviewManagerId": str(maria.id)})
    assert r.status_code == 403


async def test_users_list_has_manager_name(client):
    admin, pedro, maria = await make_user("Root", "admin"), await make_user("Pedro"), await make_user("Maria")
    pedro.interviewManagerId = maria.id
    await pedro.save()
    async with client(admin) as c:
        rows = {u["name"]: u for u in (await c.get("/users", params={"limit": 50})).json()["users"]}
    assert (rows["Pedro"]["interviewManagerId"], rows["Pedro"]["interviewManagerName"]) == (str(maria.id), "Maria")
    assert (rows["Maria"]["interviewManagerId"], rows["Maria"]["interviewManagerName"]) == (None, None)


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


async def test_unchanged_inactive_manager_still_saves(client):
    admin, pedro, maria = await make_user("Root", "admin"), await make_user("Pedro"), await make_user("Maria")
    pedro.interviewManagerId = maria.id
    await pedro.save()
    maria.isActive = False
    await maria.save()
    async with client(admin) as c:
        r = await c.put(f"/users/{pedro.id}", json={"phone": "123", "interviewManagerId": str(maria.id)})
    assert r.status_code == 200, r.text
    assert (await UserExtra.get(pedro.id)).interviewManagerId == maria.id


async def test_lookup_returns_is_active(client):
    pedro = await make_user("Pedro")
    await make_user("Gone", active=False)
    async with client(pedro) as c:
        rows = {u["name"]: u for u in (await c.get("/users/lookup")).json()["users"]}
    assert (rows["Pedro"]["isActive"], rows["Gone"]["isActive"]) == (True, False)
