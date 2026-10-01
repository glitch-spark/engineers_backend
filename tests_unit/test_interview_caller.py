"""Unit tests for per-round caller helpers (no database)."""

import os
from datetime import datetime, timezone

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from bson import ObjectId  # noqa: E402

from app.interview_caller import (  # noqa: E402
    CallerError,
    CallerIn,
    build_round_caller,
    caller_change_lines,
    caller_post_kind,
    round_caller_snapshot,
)
from app.models.interview import InterviewCaller, InterviewStageEvent  # noqa: E402

START = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
OWNER = ObjectId()
MATE = ObjectId()


def _build(**body):
    return build_round_caller(
        CallerIn(**{"enabled": True, **body}),
        starts_at=START,
        owner_id=OWNER,
        valid_coworker_ids={str(MATE), str(OWNER)},
        default_tz="America/New_York",
        previous=None,
    )


def _entry(**kw):
    base = dict(
        stage="intro",
        status="scheduled",
        at=START,
        scheduledAt=START,
        caller=InterviewCaller(enabled=True, callerName="TBD", startsAt=START, method="video", methodValue="https://meet"),
    )
    base.update(kw)
    return InterviewStageEvent(**base)


def test_build_round_caller_uses_round_start():
    caller = _build(coworkerIds=[str(MATE), str(OWNER)])
    assert caller.startsAt == START
    assert [str(i) for i in caller.coworkerIds] == [str(MATE)]  # owner dropped
    with pytest.raises(CallerError):
        _build(coworkerIds=[str(ObjectId())])
    with pytest.raises(CallerError):
        _build(method="carrier_pigeon")


def test_build_round_caller_disabled_keeps_ts():
    prev = InterviewCaller(enabled=True, slackChannelTs="1.2")
    caller = build_round_caller(
        CallerIn(enabled=False), starts_at=START, owner_id=OWNER,
        valid_coworker_ids=set(), default_tz=None, previous=prev,
    )
    assert caller.enabled is False
    assert caller.slackChannelTs == "1.2"


def test_caller_post_kind_matrix():
    s = round_caller_snapshot(_entry())
    moved = round_caller_snapshot(_entry(scheduledAt=datetime(2026, 10, 1, 15, tzinfo=timezone.utc)))
    canceled = round_caller_snapshot(_entry(status="canceled"))
    assert caller_post_kind(None, s) == "created"
    assert caller_post_kind(s, s) is None
    assert caller_post_kind(s, moved) == "updated"
    assert caller_post_kind(s, None) == "canceled"
    assert caller_post_kind(s, canceled) == "canceled"
    assert caller_post_kind(s, s, round_deleted=True) == "canceled"
    assert caller_post_kind(None, canceled) is None


def test_notes_only_change_has_no_lines():
    a = round_caller_snapshot(_entry(note="", transcript=""))
    b = round_caller_snapshot(_entry(note="new note", transcript="script"))
    assert caller_post_kind(a, b) is None
    assert caller_change_lines(a, b, fmt_time=str, label=lambda k, v: str(v)) == []


def test_change_lines_format():
    a = round_caller_snapshot(_entry())
    later = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc)
    b = round_caller_snapshot(_entry(
        scheduledAt=later,
        caller=InterviewCaller(enabled=True, callerName="Dana", startsAt=later, method="video", methodValue="https://meet"),
    ))
    lines = caller_change_lines(a, b, fmt_time=lambda d: d.strftime("%H:%M"), label=lambda k, v: str(v))
    assert lines == ["Time: 14:00 → 15:30", "Caller assigned: TBD → Dana"]


def test_cancel_kinds():
    s = round_caller_snapshot(_entry())
    off = round_caller_snapshot(_entry(caller=InterviewCaller(enabled=False)))
    assert off is None
    for kind in (
        caller_post_kind(s, round_caller_snapshot(_entry(status="canceled"))),
        caller_post_kind(s, off),
        caller_post_kind(s, s, round_deleted=True),
    ):
        assert kind == "canceled"


def test_no_cancel_reply_for_rounds_that_are_not_scheduled():
    done = round_caller_snapshot(_entry(status="completed"))
    canceled = round_caller_snapshot(_entry(status="canceled"))
    # Deleting an old interview must not post "Canceled" for calls that happened…
    assert caller_post_kind(done, done, round_deleted=True) is None
    assert caller_post_kind(done, None) is None
    # …nor a second "Canceled" for a round that already was.
    assert caller_post_kind(canceled, None) is None
    assert caller_post_kind(canceled, canceled, round_deleted=True) is None
