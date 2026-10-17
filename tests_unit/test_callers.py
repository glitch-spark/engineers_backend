"""Caller list (/callers) and caller assignment on rounds, mongomock, Slack stubbed."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, Caller, UserExtra
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent
from app.models.interview_question import InterviewQuestion
from app.routers.interviews import flatten_rounds

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["callers-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion, Caller])
    return database


@pytest.fixture
def client(db, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import callers, interviews

    monkeypatch.setattr(interviews, "_sync_caller_posts", AsyncMock())
    monkeypatch.setattr(interviews, "_after_save", AsyncMock())

    def make(user):
        app = FastAPI()
        app.include_router(interviews.router)
        app.include_router(callers.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


async def make_user(name="Pedro", role="staff") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def make_caller(name, color="sky") -> Caller:
    return await Caller(name=name, nameKey=name.lower(), color=color).insert()


async def make_interview(owner, *, caller=True, caller_name="TBD", caller_id=None) -> Interview:
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, endsAt=START.replace(hour=16), status="scheduled",
        caller=InterviewCaller(enabled=caller, callerName=caller_name, callerId=caller_id, startsAt=START, method="video"),
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[entry.model_dump()], stage="intro", status="scheduled",
    ).insert()


def rid(iv):
    return iv.stageHistory[-1].id


async def fresh(iv) -> InterviewCaller:
    return (await Interview.get(iv.id)).stageHistory[-1].caller


# ---- the list ----

async def test_seed_adds_the_starting_callers(db):
    from app.routers.callers import seed_callers

    assert await seed_callers() == 3
    names = sorted(c.name for c in await Caller.find_all().to_list())
    assert names == ["Jamil's group", "Muhammad", "Pietro"]
    assert len({c.color for c in await Caller.find_all().to_list()}) == 3
    assert await seed_callers() == 0  # names are unique


async def test_everyone_reads_the_list_only_managers_change_it(client):
    await make_caller("Pietro")
    staff, puma = await make_user("Sam"), await make_user("Puma", role="interview_manager")
    async with client(staff) as c:
        got = (await c.get("/callers")).json()
        assert [x["name"] for x in got["callers"]] == ["Pietro"] and got["canManage"] is False
        assert (await c.post("/callers", json={"name": "Zed"})).status_code == 403
        pid = got["callers"][0]["_id"]
        assert (await c.put(f"/callers/{pid}", json={"name": "P"})).status_code == 403
        assert (await c.delete(f"/callers/{pid}")).status_code == 403
    async with client(puma) as c:
        assert (await c.get("/callers")).json()["canManage"] is True
        made = await c.post("/callers", json={"name": "  Muhammad  "})
        assert made.status_code == 201
        assert made.json()["name"] == "Muhammad" and made.json()["color"] == "violet"  # first colour not in use


@pytest.mark.parametrize("name, code", [("pietro", 409), ("", 400), ("TBD", 400), ("x" * 61, 400)])
async def test_bad_or_duplicate_names_are_refused(client, name, code):
    await make_caller("Pietro")
    admin = await make_user("Admin", role="admin")
    async with client(admin) as c:
        assert (await c.post("/callers", json={"name": name})).status_code == code


async def test_rename_follows_assigned_rounds_and_delete_keeps_the_name(client):
    pietro = await make_caller("Pietro")
    owner, admin = await make_user(), await make_user("Admin", role="admin")
    iv = await make_interview(owner, caller_name="Pietro", caller_id=str(pietro.id))
    other = await make_interview(owner, caller_name="David")
    async with client(admin) as c:
        res = await c.put(f"/callers/{pietro.id}", json={"name": "Pietro R.", "color": "rose"})
        assert res.status_code == 200 and res.json()["color"] == "rose"
        assert (await c.put(f"/callers/{pietro.id}", json={"color": "black"})).status_code == 400
        assert (await fresh(iv)).callerName == "Pietro R."
        assert (await fresh(other)).callerName == "David"
        assert (await c.delete(f"/callers/{pietro.id}")).status_code == 204
    kept = await fresh(iv)
    assert (kept.callerName, kept.callerId) == ("Pietro R.", str(pietro.id))


# ---- rounds ----

async def test_creator_picks_a_caller_when_adding_a_round(client):
    pietro = await make_caller("Pietro")
    owner = await make_user()
    iv = await make_interview(owner, caller=False)
    body = {"stage": "tech_round_1", "scheduledAt": "2026-10-09T15:00:00Z",
            "caller": {"enabled": True, "callerId": str(pietro.id), "callerName": "ignored"}}
    async with client(owner) as c:
        res = await c.post(f"/interviews/{iv.id}/stages", json=body)
        assert res.status_code == 201, res.text
        caller = res.json()["stageHistory"][-1]["caller"]
        assert (caller["callerId"], caller["callerName"]) == (str(pietro.id), "Pietro")
        body["stage"], body["caller"] = "tech_round_2", {"enabled": True, "callerId": str(PydanticObjectId())}
        assert (await c.post(f"/interviews/{iv.id}/stages", json=body)).status_code == 400
        body["caller"] = {"enabled": True}  # optional: no caller yet
        caller = (await c.post(f"/interviews/{iv.id}/stages", json=body)).json()["stageHistory"][-1]["caller"]
        assert (caller["callerId"], caller["callerName"]) == (None, "TBD")


async def test_creator_cannot_change_an_assigned_caller_but_can_edit_the_round(client):
    pietro, jamil = await make_caller("Pietro"), await make_caller("Jamil's group")
    owner = await make_user()
    iv = await make_interview(owner, caller_name="Pietro", caller_id=str(pietro.id))
    url = f"/interviews/{iv.id}/stages/{rid(iv)}"
    async with client(owner) as c:
        res = await c.patch(url, json={"caller": {"enabled": True, "callerId": str(jamil.id)}})
        assert res.status_code == 403
        # The form leaves the caller out for non-managers: the assignment stays.
        res = await c.patch(url, json={"note": "n", "caller": {"enabled": True, "method": "phone_hushed"}})
        assert res.status_code == 200, res.text
    caller = await fresh(iv)
    assert (caller.callerId, caller.callerName, caller.method) == (str(pietro.id), "Pietro", "phone_hushed")


async def test_admin_changes_the_caller_in_the_round_form(client):
    jamil = await make_caller("Jamil's group")
    admin = await make_user("Admin", role="admin")
    iv = await make_interview(admin)
    async with client(admin) as c:
        res = await c.patch(f"/interviews/{iv.id}/stages/{rid(iv)}", json={"caller": {"enabled": True, "callerId": str(jamil.id)}})
        assert res.status_code == 200
    assert (await fresh(iv)).callerName == "Jamil's group"


async def test_managers_assign_callers_on_anyones_round(client):
    muhammad = await make_caller("Muhammad")
    owner, puma = await make_user(), await make_user("Puma", role="interview_manager")
    iv = await make_interview(owner)
    no_caller = await make_interview(owner, caller=False)
    url = f"/interviews/{iv.id}/stages/{rid(iv)}/caller"
    async with client(owner) as c:
        assert (await c.put(url, json={"callerId": str(muhammad.id)})).status_code == 403
    async with client(puma) as c:
        res = await c.put(url, json={"callerId": str(muhammad.id)})
        assert res.status_code == 200, res.text
        assert res.json()["stageHistory"][-1]["caller"]["callerName"] == "Muhammad"
        assert (await fresh(iv)).callerId == str(muhammad.id)
        assert (await c.put(url, json={"callerId": str(PydanticObjectId())})).status_code == 400
        res = await c.put(f"/interviews/{no_caller.id}/stages/{rid(no_caller)}/caller", json={"callerId": str(muhammad.id)})
        assert res.status_code == 400
        assert (await c.put(url, json={"callerId": ""})).status_code == 200
    caller = await fresh(iv)
    assert (caller.callerId, caller.callerName) == (None, "TBD")


# ---- calendar feed ----

def _feed_iv(*rounds):
    return {"_id": "iv1", "companyName": "Acme", "accountId": {}, "createdBy": "u1", "stageHistory": list(rounds)}


def _r(rid_, caller):
    return {"id": rid_, "stage": "intro", "status": "scheduled", "scheduledAt": datetime(2026, 10, 7, 15), "caller": caller}


def test_feed_rows_carry_the_caller_and_filter_on_it():
    ivs = [_feed_iv(
        _r("plain", None),
        _r("off", {"enabled": False, "callerName": "Pietro", "callerId": "p1"}),
        _r("tbd", {"enabled": True, "callerName": "TBD"}),
        _r("pietro", {"enabled": True, "callerName": "Pietro", "callerId": "p1"}),
        _r("legacy", {"enabled": True, "callerName": "David"}),
    )]
    span = {"from_dt": datetime(2026, 10, 5), "to_dt": datetime(2026, 10, 12)}
    every = {r["roundId"]: r for r in flatten_rounds(ivs, **span)}
    assert set(every) == {"plain", "off", "tbd", "pietro", "legacy"}
    assert (every["pietro"]["callerId"], every["pietro"]["callerName"]) == ("p1", "Pietro")
    assert (every["off"]["callerId"], every["off"]["callerName"]) == (None, None)
    ids = lambda **kw: sorted(r["roundId"] for r in flatten_rounds(ivs, **span, **kw))  # noqa: E731
    assert ids(caller_only=True) == ["legacy", "pietro", "tbd"]
    assert ids(caller_id="p1") == ["pietro"]
    assert ids(caller_id="unassigned") == ["tbd"]
