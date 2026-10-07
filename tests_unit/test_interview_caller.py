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
    round_caller_snapshot,
)
from app.models.interview import InterviewCaller, InterviewStageEvent  # noqa: E402

START = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def _build(**body):
    return build_round_caller(CallerIn(**{"enabled": True, **body}), starts_at=START, previous=None)


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
    caller = _build()
    assert caller.startsAt == START
    with pytest.raises(CallerError):
        _build(method="carrier_pigeon")


def test_caller_body_ignores_coworker_ids():
    caller = _build(coworkerIds=[str(ObjectId())])  # old clients still send them
    assert caller.enabled is True
    assert not hasattr(caller, "coworkerIds")


def test_snapshot_has_no_coworkers():
    snap = round_caller_snapshot(_entry())
    assert "coworkers" not in snap
    assert set(snap) == {"time", "stage", "status", "method", "link", "caller", "confirmed"}


def test_old_round_with_coworker_ids_loads_without_them():
    old = {
        "id": "r1", "stage": "intro", "at": START, "scheduledAt": START,
        "caller": {"enabled": True, "callerName": "Sam", "coworkerIds": [ObjectId()]},
    }
    entry = InterviewStageEvent.model_validate(old)
    assert entry.caller.callerName == "Sam"
    assert "coworkerIds" not in entry.model_dump()["caller"]


def test_build_round_caller_disabled_keeps_ts():
    prev = InterviewCaller(enabled=True, slackChannelTs="1.2")
    caller = build_round_caller(CallerIn(enabled=False), starts_at=START, previous=prev)
    assert caller.enabled is False
    assert caller.slackChannelTs == "1.2"


def test_change_lines_format():
    a = round_caller_snapshot(_entry())
    later = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc)
    b = round_caller_snapshot(_entry(
        scheduledAt=later,
        caller=InterviewCaller(enabled=True, callerName="Dana", startsAt=later, method="video", methodValue="https://meet"),
    ))
    lines = caller_change_lines(a, b, fmt_time=lambda d: d.strftime("%H:%M"), label=lambda k, v: str(v))
    assert lines == ["Time: 14:00 → 15:30", "Caller assigned: TBD → Dana"]
