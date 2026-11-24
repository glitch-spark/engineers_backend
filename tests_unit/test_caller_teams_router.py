"""Caller team admin endpoints and the caller Slack ID / team fields; mongomock, Slack faked."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, Caller, CallerTeam, Interview, UserExtra
from app.models.interview_question import InterviewQuestion

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["caller-teams-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion, Caller, CallerTeam])
    return database


@pytest.fixture
def client(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import caller_teams, callers

    def make(user):
        app = FastAPI()
        app.include_router(caller_teams.router)
        app.include_router(callers.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


async def admin() -> UserExtra:
    return await UserExtra(name="Ada", email="ada@x.io", role="admin").insert()


async def caller(name="Sam", **kw) -> Caller:
    return await Caller(name=name, nameKey=name.lower(), color="sky", **kw).insert()


def team_body(manager, **kw):
    return {"name": "Alpha", "managerCallerId": str(manager.id), "channelId": "C1", "reminderChannelId": "C2", **kw}


async def test_staff_cannot_create_team(client):
    staff = await UserExtra(name="S", email="s@x.io", role="staff").insert()
    m = await caller()
    async with client(staff) as c:
        assert (await c.post("/callers/teams", json=team_body(m))).status_code == 403


async def test_create_team_and_list_with_members(client):
    u, m = await admin(), await caller()
    async with client(u) as c:
        r = await c.post("/callers/teams", json=team_body(m))
        assert r.status_code == 201
        tid = r.json()["_id"]
        assert r.json()["dailyTime"] == "06:00" and r.json()["archived"] is False
        member = await caller("Mia")
        assert (await c.put(f"/callers/{member.id}", json={"teamId": tid})).status_code == 200
        teams = (await c.get("/callers/teams")).json()["teams"]
        assert teams[0]["memberIds"] == [str(member.id)]
        dup = await c.post("/callers/teams", json=team_body(m, name="ALPHA"))
        assert dup.status_code == 409 and dup.json()["detail"] == "A team with this name already exists."


async def test_manager_must_be_external_caller(client):
    u = await admin()
    async with client(u) as c:
        r = await c.post("/callers/teams", json={**team_body(await caller()), "managerCallerId": str(u.id)})
        assert r.status_code == 422 and r.json()["detail"] == "Pick an external caller as manager."


async def test_team_validates_time_and_timezone_and_patch(client):
    u, m = await admin(), await caller()
    async with client(u) as c:
        assert (await c.post("/callers/teams", json=team_body(m, dailyTime="25:00"))).status_code == 422
        assert (await c.post("/callers/teams", json=team_body(m, timezone="Mars/Base"))).status_code == 422
        tid = (await c.post("/callers/teams", json=team_body(m))).json()["_id"]
        r = await c.patch(f"/callers/teams/{tid}", json={"dailyTime": "07:30", "archived": True})
        assert r.json()["dailyTime"] == "07:30" and r.json()["archived"] is True
        r = await c.patch(f"/callers/teams/{tid}", json={"archived": False})
        assert r.json()["archived"] is False


async def test_set_slack_id_validates_format(client):
    u, m = await admin(), await caller()
    async with client(u) as c:
        assert (await c.put(f"/callers/{m.id}", json={"slackUserId": "abc"})).status_code == 422
        r = await c.put(f"/callers/{m.id}", json={"slackUserId": "U07Q3KD2M9A"})
        assert r.json()["slackUserId"] == "U07Q3KD2M9A"
        r = await c.put(f"/callers/{m.id}", json={"slackUserId": ""})
        assert r.json()["slackUserId"] == ""
        assert (await Caller.get(m.id)).slackUserId is None


async def test_user_caller_rejects_slack_and_team(client):
    u = await admin()
    async with client(u) as c:
        assert (await c.put(f"/callers/{u.id}", json={"slackUserId": "U07Q3KD2M9A"})).status_code == 400


async def test_set_team_on_caller_and_directory_shows_team_name(client):
    u, m = await admin(), await caller()
    async with client(u) as c:
        tid = (await c.post("/callers/teams", json=team_body(m))).json()["_id"]
        await c.put(f"/callers/{m.id}", json={"teamId": tid})
        row = [x for x in (await c.get("/callers")).json()["callers"] if x["_id"] == str(m.id)][0]
        assert row["teamId"] == tid and row["teamName"] == "Alpha" and row["slackUserId"] == ""
        urow = [x for x in (await c.get("/callers")).json()["callers"] if x["kind"] == "user"][0]
        assert urow["teamId"] == "" and urow["teamName"] == ""
        r = await c.put(f"/callers/{m.id}", json={"teamId": ""})
        assert r.json()["teamId"] == ""


async def test_archived_team_rejected_for_new_member(client):
    u, m = await admin(), await caller()
    async with client(u) as c:
        tid = (await c.post("/callers/teams", json=team_body(m))).json()["_id"]
        await c.patch(f"/callers/teams/{tid}", json={"archived": True})
        r = await c.put(f"/callers/{m.id}", json={"teamId": tid})
        assert r.status_code == 422 and r.json()["detail"] == "No such team."


async def test_connection_reports_unset_token(client, monkeypatch):
    from app.caller_teams import slack

    monkeypatch.setattr(slack, "callers_configured", lambda: False)
    async with client(await admin()) as c:
        assert (await c.get("/callers/teams/connection")).json() == {"connected": False, "workspace": None}


async def test_connection_reports_workspace(client, monkeypatch):
    from app.caller_teams import slack

    async def ws():
        return "Acme"

    monkeypatch.setattr(slack, "callers_configured", lambda: True)
    monkeypatch.setattr(slack, "workspace_name", ws)
    async with client(await admin()) as c:
        assert (await c.get("/callers/teams/connection")).json() == {"connected": True, "workspace": "Acme"}


async def test_send_test_reports_each_channel(client, monkeypatch):
    from app.caller_teams import slack

    sent = []

    async def fake(method, body):
        sent.append(body)
        if body["channel"] == "C2":
            raise RuntimeError("not_in_channel")
        return {"ok": True}

    monkeypatch.setattr(slack, "api", fake)
    u, m = await admin(), await caller()
    async with client(u) as c:
        tid = (await c.post("/callers/teams", json=team_body(m))).json()["_id"]
        r = (await c.post(f"/callers/teams/{tid}/test")).json()
    assert r == {"channel": {"ok": True, "error": None}, "reminderChannel": {"ok": False, "error": "not_in_channel"}}
    assert sent[0]["text"] == "Test post from the interview bot for Alpha."


async def test_send_test_unset_token(client, monkeypatch):
    from app.caller_teams import slack

    async def fake(method, body):
        raise RuntimeError("SLACK_CALLERS_BOT_TOKEN is not configured")

    monkeypatch.setattr(slack, "api", fake)
    u, m = await admin(), await caller()
    async with client(u) as c:
        tid = (await c.post("/callers/teams", json=team_body(m))).json()["_id"]
        r = (await c.post(f"/callers/teams/{tid}/test")).json()
    assert r["channel"] == {"ok": False, "error": "SLACK_CALLERS_BOT_TOKEN is not configured"}


async def test_staff_cannot_list_teams(client):
    staff = await UserExtra(name="S", email="s@x.io", role="staff").insert()
    async with client(staff) as c:
        assert (await c.get("/callers/teams")).status_code == 403


async def test_blank_name_and_channels_rejected(client):
    u, m = await admin(), await caller()
    async with client(u) as c:
        r = await c.post("/callers/teams", json=team_body(m, name="   "))
        assert r.status_code == 422 and r.json()["detail"] == "Team name is required."
        r = await c.post("/callers/teams", json=team_body(m, channelId=" "))
        assert r.status_code == 422 and r.json()["detail"] == "Channel ID is required."
        r = await c.post("/callers/teams", json=team_body(m, reminderChannelId=" "))
        assert r.status_code == 422 and r.json()["detail"] == "Channel ID is required."
        tid = (await c.post("/callers/teams", json=team_body(m))).json()["_id"]
        for k, msg in (("name", "Team name is required."), ("channelId", "Channel ID is required."), ("reminderChannelId", "Channel ID is required.")):
            r = await c.patch(f"/callers/teams/{tid}", json={k: "   "})
            assert r.status_code == 422 and r.json()["detail"] == msg
        assert (await CallerTeam.get(tid)).name == "Alpha"


@pytest.mark.parametrize("method,path", [
    ("get", "/callers/teams"), ("post", "/callers/teams"), ("patch", "/callers/teams/abc"),
    ("get", "/callers/teams/connection"), ("post", "/callers/teams/abc/test"),
])
async def test_staff_gets_403_on_every_team_endpoint(client, method, path):
    staff = await UserExtra(name="S", email="s@x.io", role="staff").insert()
    async with client(staff) as c:
        assert (await getattr(c, method)(path, **({"json": {}} if method != "get" else {}))).status_code == 403


async def test_slack_ids_only_shown_to_caller_managers(client):
    await caller("Sam", slackUserId="U07Q3KD2M9A")
    staff = await UserExtra(name="S", email="s@x.io", role="staff").insert()
    async with client(staff) as c:
        got = (await c.get("/callers")).json()["callers"]
    assert [x["slackUserId"] for x in got if x["kind"] == "external"] == [""]
    async with client(await admin()) as c:
        got = (await c.get("/callers")).json()["callers"]
    assert [x["slackUserId"] for x in got if x["kind"] == "external"] == ["U07Q3KD2M9A"]
