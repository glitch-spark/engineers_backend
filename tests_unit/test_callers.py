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
        assert [x["name"] for x in got["callers"] if x["kind"] == "external"] == ["Pietro"] and got["canManage"] is False
        assert (await c.post("/callers", json={"name": "Zed"})).status_code == 403
        pid = next(x["_id"] for x in got["callers"] if x["kind"] == "external")
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


async def test_rename_follows_assigned_rounds_and_archive_keeps_the_name(client):
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
        assert (await c.put(f"/callers/{pietro.id}", json={"archived": True})).status_code == 200
    kept = await fresh(iv)
    assert (kept.callerName, kept.callerId) == ("Pietro R.", str(pietro.id))


# ---- directory: users, archive, delete, colour ----

async def test_list_includes_platform_users(client):
    await make_caller("Pietro")
    staff, ana = await make_user("Sam"), await make_user("Ana", role="admin")
    async with client(staff) as c:
        got = (await c.get("/callers")).json()
    assert [x["kind"] for x in got["callers"]] == ["user", "user", "external"]
    assert [x["name"] for x in got["callers"]] == ["Ana", "Sam", "Pietro"]
    assert got["canManage"] is False
    assert all(x["archived"] is False for x in got["callers"])


async def test_archive_hides_from_list_and_unarchive_restores(client):
    pietro = await make_caller("Pietro")
    puma = await make_user("Puma", role="interview_manager")
    async with client(puma) as c:
        res = await c.put(f"/callers/{pietro.id}", json={"archived": True})
        assert res.status_code == 200 and res.json()["archived"] is True
        assert (await Caller.get(pietro.id)).archivedAt is not None
        assert "Pietro" not in [x["name"] for x in (await c.get("/callers")).json()["callers"]]
        withall = {x["name"]: x for x in (await c.get("/callers", params={"archived": 1})).json()["callers"]}
        assert withall["Pietro"]["archived"] is True
        res = await c.put(f"/callers/{pietro.id}", json={"archived": False})
        assert res.status_code == 200 and res.json()["archived"] is False
        assert (await Caller.get(pietro.id)).archivedAt is None
        assert "Pietro" in [x["name"] for x in (await c.get("/callers")).json()["callers"]]


async def test_delete_with_rounds_is_409(client):
    busy, idle = await make_caller("Busy"), await make_caller("Idle")
    owner, admin = await make_user(), await make_user("Admin", role="admin")
    await make_interview(owner, caller_name="Busy", caller_id=str(busy.id))
    async with client(admin) as c:
        res = await c.delete(f"/callers/{busy.id}")
        assert res.status_code == 409
        assert res.json()["detail"] == "This caller has rounds — archive them instead."
        assert await Caller.get(busy.id) is not None
        assert (await c.delete(f"/callers/{idle.id}")).status_code == 204
        assert await Caller.get(idle.id) is None
        assert (await c.delete(f"/callers/{owner.id}")).status_code == 400


async def test_user_caller_only_takes_colour(client):
    ana, puma = await make_user("Ana"), await make_user("Puma", role="interview_manager")
    async with client(puma) as c:
        res = await c.put(f"/callers/{ana.id}", json={"color": "teal"})
        assert res.status_code == 200, res.text
        assert res.json()["kind"] == "user" and res.json()["color"] == "teal"
        assert (await UserExtra.get(ana.id)).callerColor == "teal"
        assert (await c.put(f"/callers/{ana.id}", json={"name": "X"})).status_code == 400
        assert (await c.put(f"/callers/{ana.id}", json={"archived": True})).status_code == 400
        assert (await c.put(f"/callers/{ana.id}", json={"color": "black"})).status_code == 400
    assert (await UserExtra.get(ana.id)).name == "Ana"


async def test_user_rename_follows_onto_rounds(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import profile

    ana = await make_user("Ana")
    owner = await make_user("Owner")
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, status="scheduled",
        caller=InterviewCaller(enabled=True, callerName="Ana", callerId=str(ana.id), callerKind="user", startsAt=START),
    )
    iv = await Interview(accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
                         stageHistory=[entry.model_dump()]).insert()
    app = FastAPI()
    app.include_router(profile.router)
    app.dependency_overrides[get_current_user] = lambda: ana
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.put("/profile", json={"username": "Ana B", "email": ana.email})
        assert res.status_code == 200, res.text
    assert (await fresh(iv)).callerName == "Ana B"


async def test_admin_user_edit_rename_follows_onto_rounds(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import require_admin
    from app.routers import users

    ana, admin = await make_user("Ana"), await make_user("Admin", role="admin")
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, status="scheduled",
        caller=InterviewCaller(enabled=True, callerName="Ana", callerId=str(ana.id), callerKind="user", startsAt=START),
    )
    iv = await Interview(accountId=PydanticObjectId(), createdBy=admin.id, scheduledAt=START, companyName="Acme",
                         stageHistory=[entry.model_dump()]).insert()
    app = FastAPI()
    app.include_router(users.router)
    app.dependency_overrides[require_admin] = lambda: admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.put(f"/users/{ana.id}", json={"phone": "1"})).status_code == 200
        assert (await fresh(iv)).callerName == "Ana"
        assert (await c.put(f"/users/{ana.id}", json={"name": "Ana C"})).status_code == 200
    assert (await fresh(iv)).callerName == "Ana C"


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


async def test_assign_platform_user_sets_kind_user(client):
    ana, puma = await make_user("Ana"), await make_user("Puma", role="interview_manager")
    owner = await make_user("Owner")
    iv = await make_interview(owner)
    async with client(puma) as c:
        res = await c.put(f"/interviews/{iv.id}/stages/{rid(iv)}/caller", json={"callerId": str(ana.id)})
        assert res.status_code == 200, res.text
        caller = res.json()["stageHistory"][-1]["caller"]
        assert (caller["callerId"], caller["callerName"], caller["callerKind"]) == (str(ana.id), "Ana", "user")
    stored = await fresh(iv)
    assert stored.callerKind == "user" and stored.callerName == "Ana"


async def test_assign_external_and_unassign_set_kind_external(client):
    pietro, puma = await make_caller("Pietro"), await make_user("Puma", role="interview_manager")
    ana, owner = await make_user("Ana"), await make_user("Owner")
    iv = await make_interview(owner, caller_name="Ana", caller_id=str(ana.id))
    url = f"/interviews/{iv.id}/stages/{rid(iv)}/caller"
    async with client(puma) as c:
        assert (await c.put(url, json={"callerId": str(pietro.id)})).status_code == 200
        assert (await fresh(iv)).callerKind == "external"
        assert (await c.put(url, json={"callerId": ""})).status_code == 200
    caller = await fresh(iv)
    assert (caller.callerId, caller.callerName, caller.callerKind) == (None, "TBD", "external")


async def test_owner_cannot_be_own_caller(client):
    owner, puma = await make_user("Owner"), await make_user("Puma", role="interview_manager")
    iv = await make_interview(owner)
    copy = "You take your own interview as Self — pick someone else as the caller."
    async with client(puma) as c:
        res = await c.put(f"/interviews/{iv.id}/stages/{rid(iv)}/caller", json={"callerId": str(owner.id)})
        assert res.status_code == 400 and res.json()["detail"] == copy
    async with client(owner) as c:
        body = {"stage": "tech_round_1", "scheduledAt": "2026-10-09T15:00:00Z",
                "caller": {"enabled": True, "callerId": str(owner.id)}}
        res = await c.post(f"/interviews/{iv.id}/stages", json=body)
        assert res.status_code == 400 and res.json()["detail"] == copy
        res = await c.patch(f"/interviews/{iv.id}/stages/{rid(iv)}",
                            json={"caller": {"enabled": True, "callerId": str(owner.id)}})
        assert res.status_code == 400 and res.json()["detail"] == copy
        account = await Account(name="Erric", createdBy=owner.id).insert()
        create = {"companyName": "Acme", "accountId": str(account.id),
                  "resumeLink": None, "round": {"stage": "intro", "scheduledAt": "2026-10-09T15:00:00Z",
                                                "caller": {"enabled": True, "callerId": str(owner.id)}}}
        res = await c.post("/interviews", json=create)
        assert res.status_code == 400 and res.json()["detail"] == copy
        ana = await make_user("Ana")
        create["round"]["caller"]["callerId"] = str(ana.id)
        res = await c.post("/interviews", json=create)
        assert res.status_code == 201, res.text
        caller = res.json()["stageHistory"][-1]["caller"]
        assert (caller["callerName"], caller["callerKind"]) == ("Ana", "user")


async def test_inactive_user_is_no_longer_on_the_list(client):
    gone, puma = await make_user("Gone"), await make_user("Puma", role="interview_manager")
    gone.isActive = False
    await gone.save()
    iv = await make_interview(await make_user("Owner"))
    async with client(puma) as c:
        res = await c.put(f"/interviews/{iv.id}/stages/{rid(iv)}/caller", json={"callerId": str(gone.id)})
        assert res.status_code == 400 and res.json()["detail"] == "That caller is no longer on the list."


async def _confirm(iv):
    fresh_iv = await Interview.get(iv.id)
    fresh_iv.stageHistory[-1].confirmed = True
    await fresh_iv.save()


async def _confirmed(iv) -> bool:
    return (await Interview.get(iv.id)).stageHistory[-1].confirmed


async def test_changing_caller_clears_confirmed(client):
    ana, bob = await make_user("Ana"), await make_user("Bob")
    pietro, puma = await make_caller("Pietro"), await make_user("Puma", role="interview_manager")
    iv = await make_interview(await make_user("Owner"), caller_name="Ana", caller_id=str(ana.id))
    await _confirm(iv)
    url = f"/interviews/{iv.id}/stages/{rid(iv)}/caller"
    async with client(puma) as c:
        assert (await c.put(url, json={"callerId": str(ana.id)})).status_code == 200  # same caller
        assert await _confirmed(iv) is True
        assert (await c.put(url, json={"callerId": str(bob.id)})).status_code == 200
        assert await _confirmed(iv) is False
        await _confirm(iv)
        assert (await c.put(url, json={"callerId": str(pietro.id)})).status_code == 200
        assert await _confirmed(iv) is False


async def test_patch_changing_caller_clears_confirmed_same_caller_keeps_it(client):
    ana, bob = await make_user("Ana"), await make_user("Bob")
    admin = await make_user("Admin", role="admin")
    iv = await make_interview(await make_user("Owner"), caller_name="Ana", caller_id=str(ana.id))
    await _confirm(iv)
    url = f"/interviews/{iv.id}/stages/{rid(iv)}"
    async with client(admin) as c:
        res = await c.patch(url, json={"caller": {"enabled": True, "callerId": str(ana.id)}})
        assert res.status_code == 200, res.text
        assert await _confirmed(iv) is True
        res = await c.patch(url, json={"caller": {"enabled": True, "callerId": str(bob.id)}})
        assert res.status_code == 200, res.text
        assert await _confirmed(iv) is False
    assert (await fresh(iv)).callerKind == "user"


async def test_creator_picks_a_platform_user_when_adding_a_round(client):
    ana, owner = await make_user("Ana"), await make_user("Owner")
    iv = await make_interview(owner, caller=False)
    body = {"stage": "tech_round_1", "scheduledAt": "2026-10-09T15:00:00Z",
            "caller": {"enabled": True, "callerId": str(ana.id), "callerKind": "external"}}  # client kind is ignored
    async with client(owner) as c:
        res = await c.post(f"/interviews/{iv.id}/stages", json=body)
        assert res.status_code == 201, res.text
        caller = res.json()["stageHistory"][-1]["caller"]
        assert (caller["callerId"], caller["callerName"], caller["callerKind"]) == (str(ana.id), "Ana", "user")


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


# ---- list feed (/interviews/caller-rounds) ----

async def test_caller_rounds_list_pages_newest_first_and_filters(client):
    pietro = await make_caller("Pietro")
    owner, other = await make_user(), await make_user("Kim")
    days = [7, 8, 9]
    for d in days:  # three caller rounds on different days, two owners
        when = START.replace(day=d)
        entry = InterviewStageEvent(
            stage="intro", at=when, scheduledAt=when, status="scheduled",
            caller=InterviewCaller(enabled=True, startsAt=when, callerName="Pietro" if d == 8 else "TBD",
                                   callerId=str(pietro.id) if d == 8 else None),
        )
        await Interview(accountId=PydanticObjectId(), createdBy=(owner if d != 9 else other).id, scheduledAt=when,
                        companyName=f"Co{d}", stageHistory=[entry.model_dump()]).insert()
    await make_interview(owner, caller=False)  # not a caller round: left out
    async with client(owner) as c:
        res = (await c.get("/interviews/caller-rounds", params={"limit": 2})).json()
        assert [r["companyName"] for r in res["rounds"]] == ["Co9", "Co8"]  # every owner, newest first
        assert res["pagination"]["total"] == 3 and res["pagination"]["hasNext"] is True
        assert set(res["interviews"]) == {r["interviewId"] for r in res["rounds"]}
        page2 = (await c.get("/interviews/caller-rounds", params={"limit": 2, "page": 2})).json()
        assert [r["companyName"] for r in page2["rounds"]] == ["Co7"]
        asc = (await c.get("/interviews/caller-rounds", params={"dir": "asc"})).json()
        assert [r["companyName"] for r in asc["rounds"]] == ["Co7", "Co8", "Co9"]
        only = (await c.get("/interviews/caller-rounds", params={"callerId": str(pietro.id)})).json()
        assert [r["callerName"] for r in only["rounds"]] == ["Pietro"]
        none = (await c.get("/interviews/caller-rounds", params={"callerId": "unassigned"})).json()
        assert [r["companyName"] for r in none["rounds"]] == ["Co9", "Co7"]
        span = {"from": "2026-10-08T00:00:00Z", "to": "2026-10-08T23:59:59.999Z"}
        ranged = (await c.get("/interviews/caller-rounds", params=span)).json()
        assert [r["companyName"] for r in ranged["rounds"]] == ["Co8"]
        assert (await c.get("/interviews/caller-rounds", params={"status": "nope"})).status_code == 400


async def test_caller_rounds_list_filters_by_owner(client):
    owner, other = await make_user(), await make_user("Kim")
    await make_interview(owner)
    await make_interview(other)
    await make_interview(other)
    async with client(owner) as c:
        mine = (await c.get("/interviews/caller-rounds", params={"creatorId": str(other.id)})).json()
        assert mine["pagination"]["total"] == 2 and {r["interviewId"] for r in mine["rounds"]} == set(mine["interviews"])
        assert {i["createdBy"]["_id"] for i in mine["interviews"].values()} == {str(other.id)}
        every = (await c.get("/interviews/caller-rounds")).json()
        assert every["pagination"]["total"] == 3
        assert (await c.get("/interviews/caller-rounds", params={"creatorId": "nope"})).status_code == 400
