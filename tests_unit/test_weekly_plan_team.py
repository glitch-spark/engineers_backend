"""Admin team read and bulk save of weekly plans (mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.models.plan_common import Counts, PlanItem
from app.models.weekly_plan import WeeklyPlan
from app.deps import require_admin
from app.routers.weekly_plans import get_team_week, put_team_week

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

WEEK, PREV = "2026-11-09", "2026-11-02"


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["weekly-team-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, WeeklyPlan, Interview, InterviewQuestion])


@pytest.fixture
async def admin(db):
    return await UserExtra(name="Root", email="r@x.io", role="admin").insert()


@pytest.fixture
async def ana(db):
    return await UserExtra(name="ana", email="a@x.io").insert()


@pytest.fixture
async def bo(db):
    return await UserExtra(name="Bo", email="b@x.io").insert()


@pytest.fixture
async def cy(db):
    return await UserExtra(name="Cy", email="c@x.io").insert()


async def plan(user, week, **kw):
    d = datetime.fromisoformat(week)
    return await WeeklyPlan(userId=user.id, weekStart=d, startDate=d, endDate=d, year=2026, weekNumber=45, **kw).insert()


def row(user, **kw):
    r = {"userId": str(user.id), "plansOwnWeek": False, "goal": {"bidsSelf": 0, "bidsBidder": 0, "interviewsSelf": 0},
         "routines": [], "weeklyGoals": []}
    r.update(kw)
    return r


async def put(admin, *rows):
    return await put_team_week(WEEK, admin, {"plans": list(rows)})


async def stored(user):
    return await WeeklyPlan.find({"userId": user.id}).to_list()


async def test_team_read_saved_draft_and_empty(db, admin, ana, bo, cy):
    await UserExtra(name="Gone", email="g@x.io", isActive=False).insert()
    await plan(ana, WEEK, goal=Counts(bidsSelf=10, bidsBidder=3, interviewsSelf=2),
               routines=[PlanItem(text="live"), PlanItem(text="old", archived=True)])
    await plan(bo, PREV, goal=Counts(bidsSelf=7), routines=[PlanItem(text="r")])
    out = await get_team_week(WEEK, admin)
    users = out["users"]
    assert out["weekStart"] == WEEK and [u["name"] for u in users] == ["ana", "Bo", "Cy"]
    a, b, c = users
    assert a["exists"] is True and a["goal"] == {"bidsSelf": 10, "bidsBidder": 3, "interviewsSelf": 2}
    assert [r["text"] for r in a["routines"]] == ["live"]
    assert b["exists"] is False and b["goal"]["bidsSelf"] == 7 and [r["text"] for r in b["routines"]] == ["r"]
    assert c["exists"] is False and c["routines"] == [] and c["weeklyGoals"] == [] and c["goal"]["bidsSelf"] == 0


async def test_team_routes_require_admin():
    from app.routers.weekly_plans import router
    paths = {"GET": 0, "PUT": 0}
    for r in router.routes:
        if r.path == "/weekly-plans/team/{week_start}":
            assert any(d.call is require_admin for d in r.dependant.dependencies)
            paths[next(iter(r.methods))] += 1
    assert paths == {"GET": 1, "PUT": 1}


async def test_team_save_creates_and_updates(db, admin, ana, bo):
    await plan(ana, WEEK, routines=[PlanItem(text="a")])
    draft = await plan(bo, PREV, routines=[PlanItem(text="b")])
    cur = (await get_team_week(WEEK, admin))["users"]
    ra, rb = cur
    ra["routines"].append({"text": "new"})
    out = await put(admin, ra, rb)
    assert [r["ok"] for r in out["results"]] == [True, True]
    assert [r.text for r in (await stored(ana))[0].routines] == ["a", "new"]
    (bp,) = [p for p in await stored(bo) if p.weekStart == datetime.fromisoformat(WEEK)]
    assert [r.id for r in bp.routines] == [draft.routines[0].id]


async def test_team_save_twice_is_the_same(db, admin, ana):
    await plan(ana, WEEK, goal=Counts(bidsSelf=5), routines=[PlanItem(text="a")],
               weeklyGoals=[PlanItem(text="g", kind="count", target=3)])
    body = (await get_team_week(WEEK, admin))["users"]
    for _ in range(2):
        out = await put(admin, *body)
        assert out["results"][0]["ok"] is True
    (p,) = await stored(ana)
    assert [r.id for r in p.routines] == [body[0]["routines"][0]["id"]]
    assert not any(i.archived for i in p.routines + p.weeklyGoals)


async def test_team_save_archives_removed_and_keeps_hidden_fields(db, admin, ana):
    keep, drop, old = PlanItem(text="keep"), PlanItem(text="drop"), PlanItem(text="old", archived=True)
    await plan(ana, WEEK, recapNotes="r", goal=Counts(interviewsCaller=4), routines=[keep, drop, old])
    await put(admin, row(ana, routines=[{"id": keep.id, "text": "keep"}]))
    (p,) = await stored(ana)
    assert {r.id: r.archived for r in p.routines} == {keep.id: False, drop.id: True, old.id: True}
    assert p.recapNotes == "r" and p.goal.interviewsCaller == 4


async def test_team_save_keeps_split_when_total_unchanged(db, admin, ana):
    await plan(ana, WEEK, goal=Counts(bidsSelf=600, bidsSelfUs=400, bidsSelfLatam=200))
    await put(admin, row(ana, goal={"bidsSelf": 600, "bidsBidder": 1, "interviewsSelf": 2}))
    g = (await stored(ana))[0].goal
    assert (g.bidsSelf, g.bidsSelfUs, g.bidsSelfLatam, g.bidsBidder, g.interviewsSelf) == (600, 400, 200, 1, 2)
    await put(admin, row(ana, goal={"bidsSelf": 650, "bidsBidder": 0, "interviewsSelf": 0}))
    g = (await stored(ana))[0].goal
    assert (g.bidsSelf, g.bidsSelfUs, g.bidsSelfEu, g.bidsSelfLatam) == (650, 0, 0, 0)


async def test_team_save_writes_the_switch(db, admin, ana, monkeypatch):
    await put(admin, row(ana, plansOwnWeek=True))
    assert (await UserExtra.get(ana.id)).plansOwnWeek is True
    calls = []
    orig = UserExtra.find_one
    monkeypatch.setattr(UserExtra, "find_one", classmethod(
        lambda cls, *a, **k: (calls.append(a) if not isinstance(a[0], dict) else None) or orig(*a, **k)))  # dict = a get()
    await put(admin, row(ana, plansOwnWeek=True))
    assert calls == []


async def test_team_save_row_failures_do_not_stop_the_rest(db, admin, ana, bo):
    out = await put(
        admin,
        row(ana, plansOwnWeek=False) | {"userId": "6" * 24},
        row(admin),
        row(bo, routines=[{"text": f"r{i}"} for i in range(21)]),
        row(ana, goal={"bidsSelf": 5, "bidsBidder": 0, "interviewsSelf": 0}),
    )
    r = out["results"]
    assert [x["ok"] for x in r] == [False, False, False, True]
    assert r[0]["error"] == "User not found" and r[1]["error"] == "Admins have no plan"
    assert r[2]["error"] == "at most 20 checklist items"
    assert (await stored(ana))[0].goal.bidsSelf == 5


async def test_team_save_refuses_a_repeated_row(db, admin, ana):
    out = await put(admin, row(ana, goal={"bidsSelf": 5, "bidsBidder": 0, "interviewsSelf": 0}),
                    row(ana, goal={"bidsSelf": 9, "bidsBidder": 0, "interviewsSelf": 0}))
    assert [(x["ok"], x["error"]) for x in out["results"]] == [(True, None), (False, "Repeated row for this person")]
    assert (await stored(ana))[0].goal.bidsSelf == 5


async def test_team_save_skips_empty_row_without_a_plan(db, admin, cy):
    out = await put(admin, row(cy, plansOwnWeek=True))
    assert out["results"][0]["ok"] is True
    assert await stored(cy) == [] and (await UserExtra.get(cy.id)).plansOwnWeek is True


async def test_team_save_retype_is_remove_plus_add(db, admin, ana):
    r1 = PlanItem(text="same")
    await plan(ana, WEEK, routines=[r1])
    await put(admin, row(ana, routines=[{"text": "same", "kind": "check"}]))
    (p,) = await stored(ana)
    assert [(r.id == r1.id, r.archived) for r in p.routines] == [(False, False), (True, True)]


async def test_team_save_skips_empty_row_for_draft_only(db, admin, bo):
    await plan(bo, PREV, goal=Counts(bidsSelf=7), routines=[PlanItem(text="r")])
    out = await put(admin, row(bo, plansOwnWeek=True))
    assert out["results"][0]["ok"] is True
    assert [p.weekStart.isoformat()[:10] for p in await stored(bo)] == [PREV]
    assert (await UserExtra.get(bo.id)).plansOwnWeek is True


async def test_team_save_partial_row_fails_and_changes_nothing(db, admin, ana):
    await plan(ana, WEEK, goal=Counts(bidsSelf=5), routines=[PlanItem(text="a")])
    out = await put(admin, {"userId": str(ana.id)})
    assert out["results"][0]["ok"] is False
    (p,) = await stored(ana)
    assert p.goal.bidsSelf == 5 and [(r.text, r.archived) for r in p.routines] == [("a", False)]
    assert (await UserExtra.get(ana.id)).plansOwnWeek is False


async def test_team_save_carry_over_row_does_not_overwrite_a_plan_saved_since(db, admin, ana):
    await UserExtra.find_one(UserExtra.id == ana.id).update({"$set": {"plansOwnWeek": True}})
    mine = await plan(ana, WEEK, goal=Counts(bidsSelf=9), routines=[PlanItem(text="mine")])
    out = await put(admin, row(ana, exists=False, goal={"bidsSelf": 3, "bidsBidder": 0, "interviewsSelf": 0},
                               routines=[{"text": "draft"}]))
    assert out["results"][0] == {"userId": str(ana.id), "ok": False,
                                 "error": "Saved since you loaded the grid — reload to see it"}
    (p,) = await stored(ana)
    assert p.goal.bidsSelf == 9 and [(r.id, r.archived) for r in p.routines] == [(mine.routines[0].id, False)]
    assert (await UserExtra.get(ana.id)).plansOwnWeek is True


async def test_team_save_carry_over_row_over_a_draft_saves(db, admin, bo):
    await plan(bo, PREV, routines=[PlanItem(text="r")])
    out = await put(admin, row(bo, exists=False, goal={"bidsSelf": 3, "bidsBidder": 0, "interviewsSelf": 0}))
    assert out["results"][0]["ok"] is True
    (p,) = [p for p in await stored(bo) if p.weekStart == datetime.fromisoformat(WEEK)]
    assert p.goal.bidsSelf == 3


async def test_team_save_kind_change_with_kept_id_is_remove_plus_add(db, admin, ana):
    r1 = PlanItem(text="calls", kind="count", target=5)
    await plan(ana, WEEK, routines=[r1])
    await put(admin, row(ana, routines=[{"id": r1.id, "text": "calls", "kind": "log"}]))
    (p,) = await stored(ana)
    assert [(r.id == r1.id, r.kind, r.archived) for r in p.routines] == [(False, "log", False), (True, "count", True)]


async def test_team_save_keeps_added_by(db, admin, ana):
    s1 = PlanItem(text="mine", addedBy="self")
    await plan(ana, WEEK, routines=[PlanItem(text="a"), s1])
    r = row(ana, routines=[{"id": s1.id, "text": "mine 2", "addedBy": "admin"}, {"text": "new"}])
    await put(admin, r)
    (p,) = await stored(ana)
    assert [(i.text, i.addedBy) for i in p.routines] == [("mine 2", "self"), ("new", "admin"), ("a", "admin")]


async def test_team_read_returns_added_by(db, admin, ana):
    await plan(ana, WEEK, routines=[PlanItem(text="a"), PlanItem(text="b", addedBy="self")])
    (u,) = (await get_team_week(WEEK, admin))["users"]
    assert [r["addedBy"] for r in u["routines"]] == ["admin", "self"]


async def test_team_save_stale_row_keeps_new_own_item(db, admin, ana):
    a, s = PlanItem(text="a"), PlanItem(text="Discord", addedBy="self")
    await plan(ana, WEEK, routines=[a, s])
    await put(admin, row(ana, routines=[{"id": a.id, "text": "a"}]))  # loaded before "Discord" existed
    p = (await stored(ana))[0]
    assert [(i.text, i.archived) for i in p.routines] == [("a", False), ("Discord", False)]


async def test_team_save_removed_ids_archives_own_item(db, admin, ana):
    a, s = PlanItem(text="a"), PlanItem(text="Discord", addedBy="self")
    await plan(ana, WEEK, routines=[a, s])
    await put(admin, row(ana, routines=[{"id": a.id, "text": "a"}], removedIds=[s.id]))
    p = (await stored(ana))[0]
    assert [(i.text, i.archived) for i in p.routines] == [("a", False), ("Discord", True)]


async def test_team_save_missing_admin_item_is_still_archived(db, admin, ana):
    a, b = PlanItem(text="a"), PlanItem(text="b")
    await plan(ana, WEEK, routines=[a, b])
    await put(admin, row(ana, routines=[{"id": a.id, "text": "a"}]))
    p = (await stored(ana))[0]
    assert [(i.text, i.archived) for i in p.routines] == [("a", False), ("b", True)]


async def test_team_save_round_trips_options_and_edit_keeps_id(db, admin, ana):
    await put(admin, row(ana, routines=[{"text": "State", "kind": "choice", "options": ["Done", "Blocked"]}]))
    (p,) = await stored(ana)
    r = p.routines[0]
    assert r.options == ["Done", "Blocked"]
    await put(admin, row(ana, routines=[{"id": r.id, "text": "State", "kind": "choice", "options": ["Yes", "No", "N/A"]}]))
    (p,) = await stored(ana)
    assert [(x.id == r.id, x.options, x.archived) for x in p.routines] == [(True, ["Yes", "No", "N/A"], False)]


async def test_team_save_kind_change_to_and_from_choice_is_remove_plus_add(db, admin, ana):
    r1 = PlanItem(text="s", kind="check")
    await plan(ana, WEEK, routines=[r1])
    await put(admin, row(ana, routines=[{"id": r1.id, "text": "s", "kind": "choice", "options": ["a", "b"]}]))
    (p,) = await stored(ana)
    new = p.routines[0]
    assert (new.id == r1.id, new.kind, p.routines[1].archived) == (False, "choice", True)
    await put(admin, row(ana, routines=[{"id": new.id, "text": "s", "kind": "text"}]))
    (p,) = await stored(ana)
    assert (p.routines[0].id == new.id, p.routines[0].kind, p.routines[0].options) == (False, "text", [])


async def test_team_save_rejects_bad_options(db, admin, ana):
    out = await put(admin, row(ana, routines=[{"text": "s", "kind": "choice", "options": ["only"]}]))
    assert out["results"][0]["ok"] is False and await stored(ana) == []
