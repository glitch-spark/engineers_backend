"""scripts/backfill_round_confirmed: assigned caller rounds still unconfirmed get confirmed=True (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import argparse
from datetime import datetime, timedelta, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, Caller, UserExtra
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent
from app.models.interview_question import InterviewQuestion
from scripts import backfill_round_confirmed as bf

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
OWNER = PydanticObjectId()


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["backfill-confirmed-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion, Caller])
    return database


def _round(at, *, name="Ana", caller_id="u1", kind="user", enabled=True, confirmed=False, status="scheduled",
           stage="intro"):
    caller = None if name is None else InterviewCaller(
        enabled=enabled, callerName=name, callerId=caller_id, callerKind=kind, startsAt=at, method="video",
    )
    return InterviewStageEvent(
        stage=stage, at=at, scheduledAt=at, endsAt=at + timedelta(hours=1), status=status, confirmed=confirmed,
        caller=caller,
    ).model_dump()


async def make_interview(company, rounds) -> Interview:
    return await Interview(
        accountId=PydanticObjectId(), createdBy=OWNER, scheduledAt=NOW, companyName=company, stageHistory=rounds,
        stage="intro", status="scheduled",
    ).insert()


def args(apply=False, days=7):
    return argparse.Namespace(apply=apply, days=days)


async def flags(iv) -> list[bool]:
    saved = await Interview.get(iv.id)
    return [r.confirmed for r in saved.stageHistory]


async def test_dry_run_lists_by_interview_and_writes_nothing_apply_confirms(db, capsys):
    soon, recent, old = NOW + timedelta(days=1), NOW - timedelta(days=3), NOW - timedelta(days=8)
    acme = await make_interview("Acme", [
        _round(old),  # too old
        _round(recent, stage="tech_round_1", name="Pietro", caller_id="c1", kind="external"),  # external: picked
        _round(soon, stage="tech_round_2"),  # user, latest: picked
    ])
    globex = await make_interview("Globex", [_round(soon, name="Sam", caller_id=None, kind="external")])  # typed name
    skipped = await make_interview("Skipped", [
        _round(soon, name="TBD", caller_id=None, kind="external"),  # not assigned
        _round(soon + timedelta(hours=1), stage="tech_round_1", name="", caller_id=None),  # not assigned
        _round(soon + timedelta(hours=2), stage="tech_round_2", enabled=False),  # Self / off
        _round(soon + timedelta(hours=3), stage="final", status="canceled"),  # called off
        _round(soon + timedelta(hours=4), stage="tech_round_3", confirmed=True),  # already confirmed
        _round(soon + timedelta(hours=5), stage="intro", name=None),  # no caller at all
    ])

    assert await bf.run(args(), now=NOW) == 0
    out = capsys.readouterr().out
    assert "Rounds to confirm: 3 in 2 interview(s)" in out
    assert "Acme" in out and "Globex" in out and "Skipped" not in out
    assert out.index("Acme") < out.index("caller Pietro") < out.index("caller Ana") < out.index("Globex")
    assert "DRY RUN" in out
    assert await flags(acme) == [False, False, False] and await flags(globex) == [False]

    assert await bf.run(args(apply=True), now=NOW) == 0
    assert "APPLIED: confirmed 3 round(s)" in capsys.readouterr().out
    assert await flags(acme) == [False, True, True]
    assert (await Interview.get(acme.id)).confirmed is True  # the latest round's mirror
    assert await flags(globex) == [True]
    assert await flags(skipped) == [False, False, False, False, True, False]

    # Idempotent: nothing left.
    assert await bf.run(args(apply=True), now=NOW) == 0
    assert "Rounds to confirm: 0 in 0 interview(s)" in capsys.readouterr().out


async def test_days_widens_the_window(db, capsys):
    iv = await make_interview("Acme", [_round(NOW - timedelta(days=10))])
    assert await bf.plan_backfill(NOW) == []
    (target,) = await bf.plan_backfill(NOW, days=14)
    assert target.interview_id == iv.id and len(target.rounds) == 1


async def test_round_changed_since_the_scan_is_skipped(db):
    iv = await make_interview("Acme", [_round(NOW + timedelta(days=1))])
    targets = await bf.plan_backfill(NOW)
    saved = await Interview.get(iv.id)
    saved.stageHistory[0].id = "replaced"  # the round was replaced between the scan and the write
    await saved.save()
    assert await bf.apply_backfill(targets) == 0
    assert await flags(iv) == [False]
