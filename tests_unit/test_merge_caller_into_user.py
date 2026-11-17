"""scripts/merge_caller_into_user: rounds of an external caller move to a platform user (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import argparse
from datetime import datetime, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, Caller, UserExtra
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent
from app.models.interview_question import InterviewQuestion
from scripts import merge_caller_into_user as mc

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
_n = iter(range(10**6))


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["merge-caller-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion, Caller])
    return database


async def make_user(name="Nicholas", **kw) -> UserExtra:
    return await UserExtra(name=name, email=f"u{next(_n)}@x.io", role="staff", **kw).insert()


async def make_caller(name="Nicholas") -> Caller:
    return await Caller(name=name, nameKey=name.lower()).insert()


def _round(caller_id, name, kind="external", stage="intro", confirmed=False):
    return InterviewStageEvent(
        stage=stage, at=START, scheduledAt=START, endsAt=START.replace(hour=16), status="scheduled",
        confirmed=confirmed,
        caller=InterviewCaller(enabled=True, callerName=name, callerId=caller_id, callerKind=kind, startsAt=START, method="video"),
    ).model_dump()


async def make_interview(owner, company, rounds, top=None) -> Interview:
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName=company,
        stageHistory=rounds, stage="intro", status="scheduled", caller=top,
    ).insert()


async def test_dry_run_counts_and_changes_nothing_apply_moves_the_rounds_and_archives(db):
    owner, nick = await make_user("Pedro"), await make_user("Nicholas")
    ext = await make_caller("Nicholas")
    cid = str(ext.id)
    a = await make_interview(owner, "Acme", [_round(cid, "Nicholas")])
    b = await make_interview(owner, "Globex", [_round(cid, "Nicholas")])

    assert await mc.merge_caller_into_user(cid, str(nick.id), apply=False) == 2
    for iv in (a, b):
        r = (await Interview.get(iv.id)).stageHistory[0].caller
        assert (r.callerId, r.callerKind, r.callerName) == (cid, "external", "Nicholas")
    assert (await Caller.get(ext.id)).archivedAt is None

    assert await mc.merge_caller_into_user(cid, str(nick.id), apply=True) == 2
    for iv in (a, b):
        r = (await Interview.get(iv.id)).stageHistory[0].caller
        assert (r.callerId, r.callerKind, r.callerName) == (str(nick.id), "user", "Nicholas")
    assert (await Caller.get(ext.id)).archivedAt is not None
    assert await mc.merge_caller_into_user(cid, str(nick.id), apply=True) == 0  # idempotent


async def test_only_the_matching_caller_fields_change(db):
    owner, nick = await make_user("Pedro"), await make_user("Nicholas Smith")
    ext, other = await make_caller("Nicholas"), await make_caller("Pietro")
    cid, oid = str(ext.id), str(other.id)
    iv = await make_interview(owner, "Acme", [
        _round(oid, "Pietro", stage="intro"),
        _round(cid, "Nicholas", stage="technical", confirmed=True),
        _round(cid, "Nicholas", stage="final"),
    ])

    assert await mc.merge_caller_into_user(cid, str(nick.id), apply=True) == 2
    got = await Interview.get(iv.id)
    first, second, third = got.stageHistory
    assert (first.caller.callerId, first.caller.callerKind, first.caller.callerName) == (oid, "external", "Pietro")
    assert second.caller.callerName == "Nicholas Smith" and second.caller.callerKind == "user"
    assert third.caller.callerId == str(nick.id)
    # everything else on the rounds is untouched, confirmation included
    assert second.confirmed is True and third.confirmed is False
    assert second.stage == "technical" and second.caller.method == "video" and second.caller.startsAt is not None
    assert second.caller.enabled is True
    assert (await Caller.get(other.id)).archivedAt is None


async def test_top_level_caller_mirrors_the_latest_round_and_moves_with_it(db):
    owner, nick = await make_user("Pedro"), await make_user("Nicholas")
    ext = await make_caller("Nicholas")
    cid = str(ext.id)
    iv = await make_interview(owner, "Acme", [_round(cid, "Nicholas")])
    assert (await Interview.get(iv.id)).caller.callerId == cid  # the model mirrors the tip round

    assert await mc.merge_caller_into_user(cid, str(nick.id), apply=False) == 1  # one round, not two
    assert await mc.merge_caller_into_user(cid, str(nick.id), apply=True) == 1
    got = await Interview.get(iv.id)
    assert got.caller.callerId == str(nick.id) and got.caller.callerKind == "user"
    assert got.stageHistory[0].caller.callerId == str(nick.id)


async def test_legacy_document_with_only_a_top_level_caller_counts_one_and_moves(db):
    owner, nick = await make_user("Pedro"), await make_user("Nicholas")
    ext = await make_caller("Nicholas")
    cid = str(ext.id)
    res = await Interview.get_motor_collection().insert_one({
        "accountId": PydanticObjectId(), "createdBy": owner.id, "scheduledAt": START, "companyName": "Legacy Co",
        "stageHistory": [], "caller": {"enabled": True, "callerName": "Nicholas", "callerId": cid},
    })

    assert await mc.merge_caller_into_user(cid, str(nick.id), apply=True) == 1
    raw = await Interview.get_motor_collection().find_one({"_id": res.inserted_id})
    assert raw["caller"]["callerId"] == str(nick.id) and raw["caller"]["callerKind"] == "user"
    assert raw["caller"]["callerName"] == "Nicholas" and raw["stageHistory"] == [] and raw["companyName"] == "Legacy Co"


async def test_owner_is_never_made_the_caller_of_their_own_interview(db):
    nick, owner = await make_user("Nicholas"), await make_user("Pedro")
    ext = await make_caller("Nicholas")
    cid = str(ext.id)
    mine = await make_interview(nick, "Mine Inc", [_round(cid, "Nicholas")])
    theirs = await make_interview(owner, "Acme", [_round(cid, "Nicholas")])

    plan = await mc.plan_merge(cid, str(nick.id))
    assert plan.rounds == 1 and plan.companies == ["Acme"] and plan.skipped_companies == ["Mine Inc"]
    assert await mc.merge_caller_into_user(cid, str(nick.id), apply=True) == 1
    assert (await Interview.get(mine.id)).stageHistory[0].caller.callerKind == "external"
    assert (await Interview.get(theirs.id)).stageHistory[0].caller.callerKind == "user"


async def test_own_to_self_turns_the_users_own_interviews_into_self_rounds(db):
    nick, owner = await make_user("Nicholas"), await make_user("Pedro")
    ext = await make_caller("Nicholas")
    cid = str(ext.id)
    mine = await make_interview(nick, "Mine Inc", [_round(cid, "Nicholas", confirmed=True)])
    theirs = await make_interview(owner, "Acme", [_round(cid, "Nicholas")])

    plan = await mc.plan_merge(cid, str(nick.id))
    assert plan.self_rounds == 1 and plan.skipped_companies == ["Mine Inc"]
    assert await mc.merge_caller_into_user(cid, str(nick.id), apply=True, own_to_self=True) == 1
    own = (await Interview.get(mine.id)).stageHistory[0]
    assert own.caller.enabled is False and own.caller.callerId is None and own.caller.callerName == "TBD"
    assert own.confirmed is True                                   # nothing else on the round changes
    moved = (await Interview.get(theirs.id)).stageHistory[0].caller
    assert (moved.enabled, moved.callerKind, moved.callerId) == (True, "user", str(nick.id))
    # Without the option the user's own rounds stay on the external caller (the default behaviour).
    other = await make_interview(nick, "Other Inc", [_round(cid, "Nicholas")])
    await mc.merge_caller_into_user(cid, str(nick.id), apply=True)
    assert (await Interview.get(other.id)).stageHistory[0].caller.callerId == cid


async def test_cli_own_to_self_reports_and_applies(db, capsys):
    nick, owner = await make_user("Nicholas"), await make_user("Pedro")
    ext = await make_caller("Nicholas")
    mine = await make_interview(nick, "Mine Inc", [_round(str(ext.id), "Nicholas")])
    await make_interview(owner, "Acme", [_round(str(ext.id), "Nicholas")])

    assert await mc.run(_args(caller_name="Nicholas", user_name="Nicholas", own_to_self=True)) == 0
    out = capsys.readouterr().out
    assert "Switch to Self (the user owns these interviews): Mine Inc" in out and "DRY RUN" in out
    assert (await Interview.get(mine.id)).stageHistory[0].caller.enabled is True   # dry run wrote nothing
    assert await mc.run(_args(caller_name="Nicholas", user_name="Nicholas", own_to_self=True, apply=True)) == 0
    assert "and switched 1 round(s) to Self" in capsys.readouterr().out
    assert (await Interview.get(mine.id)).stageHistory[0].caller.enabled is False


async def test_unknown_ids_raise(db):
    nick, ext = await make_user(), await make_caller()
    with pytest.raises(ValueError):
        await mc.merge_caller_into_user(str(PydanticObjectId()), str(nick.id), apply=False)
    with pytest.raises(ValueError):
        await mc.merge_caller_into_user(str(ext.id), str(PydanticObjectId()), apply=False)
    with pytest.raises(ValueError):
        await mc.merge_caller_into_user("not-an-id", str(nick.id), apply=False)


def _args(**kw):
    base = dict(caller_name=None, user_name=None, caller_id=None, user_id=None, apply=False, own_to_self=False)
    base.update(kw)
    return argparse.Namespace(**base)


async def test_cli_dry_run_prints_both_people_the_count_and_companies_and_writes_nothing(db, capsys):
    owner, nick = await make_user("Pedro"), await make_user("Nicholas")
    ext = await make_caller("Nicholas")
    await make_interview(owner, "Acme", [_round(str(ext.id), "Nicholas")])
    await make_interview(owner, "Globex", [_round(str(ext.id), "Nicholas")])

    assert await mc.run(_args(caller_name="NICHOLAS", user_name="nicholas")) == 0
    out = capsys.readouterr().out
    for needle in ("Nicholas", str(ext.id), str(nick.id), "2", "Acme", "Globex", "DRY RUN"):
        assert needle in out
    assert (await Caller.get(ext.id)).archivedAt is None
    assert (await Interview.find_all().to_list())[0].stageHistory[0].caller.callerKind == "external"

    assert await mc.run(_args(caller_name="Nicholas", user_name="Nicholas", apply=True)) == 0
    assert "APPLIED" in capsys.readouterr().out
    assert (await Caller.get(ext.id)).archivedAt is not None


async def test_cli_ambiguous_or_missing_names_exit_nonzero_without_changes(db, capsys):
    owner = await make_user("Pedro")
    ext = await make_caller("Nicholas")
    one, two = await make_user("nicholas"), await make_user("NICHOLAS")  # no exact match for "Nicholas": ambiguous
    iv = await make_interview(owner, "Acme", [_round(str(ext.id), "Nicholas")])

    assert await mc.run(_args(caller_name="Nicholas", user_name="Nicholas", apply=True)) != 0
    out = capsys.readouterr().out
    assert str(one.id) in out and str(two.id) in out
    assert await mc.run(_args(caller_name="Nobody", user_name="Pedro", apply=True)) != 0  # no such caller
    assert await mc.run(_args(caller_name="Nicholas", user_name="Nobody", apply=True)) != 0  # no such user
    assert (await Caller.get(ext.id)).archivedAt is None
    assert (await Interview.get(iv.id)).stageHistory[0].caller.callerKind == "external"


async def test_cli_exact_user_name_wins_over_case_variants_and_ids_override(db):
    owner = await make_user("Pedro")
    ext = await make_caller("Nicholas")
    exact, _variant = await make_user("Nicholas"), await make_user("NICHOLAS")
    await make_interview(owner, "Acme", [_round(str(ext.id), "Nicholas")])

    assert await mc.run(_args(caller_name="Nicholas", user_name="Nicholas")) == 0  # exact is unique
    # explicit ids override names (these names match nothing)
    assert await mc.run(_args(caller_name="zzz", user_name="zzz", caller_id=str(ext.id), user_id=str(exact.id), apply=True)) == 0
    got = (await Interview.find_all().to_list())[0].stageHistory[0].caller
    assert got.callerId == str(exact.id)
