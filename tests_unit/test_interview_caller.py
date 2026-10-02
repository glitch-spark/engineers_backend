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
        valid_coworker_ids=set(), previous=prev,
    )
    assert caller.enabled is False
    assert caller.slackChannelTs == "1.2"


def test_caller_post_kind_without_a_post():
    s = round_caller_snapshot(_entry())
    canceled = round_caller_snapshot(_entry(status="canceled"))
    rejected = round_caller_snapshot(_entry(status="rejected"))
    assert caller_post_kind(None, s, has_post=False) == "created"
    # A failed first post is retried on the next real change.
    moved = round_caller_snapshot(_entry(scheduledAt=datetime(2026, 10, 1, 15, tzinfo=timezone.utc)))
    assert caller_post_kind(s, moved, has_post=False) == "created"
    assert caller_post_kind(s, s, has_post=False) is None
    assert caller_post_kind(None, canceled, has_post=False) is None
    assert caller_post_kind(None, rejected, has_post=False) is None
    assert caller_post_kind(s, s, has_post=False, round_deleted=True) is None


def test_caller_post_kind_with_a_post():
    s = round_caller_snapshot(_entry())
    moved = round_caller_snapshot(_entry(scheduledAt=datetime(2026, 10, 1, 15, tzinfo=timezone.utc)))
    canceled = round_caller_snapshot(_entry(status="canceled"))
    rejected = round_caller_snapshot(_entry(status="rejected"))
    assert caller_post_kind(s, s, has_post=True) is None
    assert caller_post_kind(s, moved, has_post=True) == "updated"
    assert caller_post_kind(s, canceled, has_post=True) == "canceled"
    assert caller_post_kind(s, rejected, has_post=True) == "canceled"
    assert caller_post_kind(s, None, has_post=True) == "canceled"  # caller turned off
    assert caller_post_kind(canceled, s, has_post=True) == "restored"
    assert caller_post_kind(None, s, has_post=True) == "restored"  # caller turned back on
    assert caller_post_kind(canceled, None, has_post=True) is None


def test_deleting_a_round_removes_its_post():
    s = round_caller_snapshot(_entry())
    done = round_caller_snapshot(_entry(status="completed"))
    for prev in (s, done, None):
        assert caller_post_kind(prev, prev, has_post=True, round_deleted=True) == "deleted"


def test_notes_only_change_has_no_lines():
    a = round_caller_snapshot(_entry(note="", transcript=""))
    b = round_caller_snapshot(_entry(note="new note", transcript="script"))
    assert caller_post_kind(a, b, has_post=True) is None
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
