"""Unit tests for round-level helpers in the interviews router (no database)."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.models.interview import InterviewCaller, InterviewStageEvent, InterviewStatus  # noqa: E402
from app.routers.interviews import _sync_top_from_tip, mark_previous_passed, round_ends_at  # noqa: E402

AT = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)


def _round(status, stage="intro", **kw):
    return InterviewStageEvent(stage=stage, at=AT, scheduledAt=AT, status=status, **kw)


def test_mark_previous_passed_only_from_scheduled_or_completed():
    for start, changed in (("scheduled", True), ("completed", True), ("passed", False), ("rejected", False), ("canceled", False)):
        history = [_round(start)]
        assert mark_previous_passed(history) is changed
        expected = InterviewStatus.passed if changed else InterviewStatus(start)
        assert history[0].status == expected
    assert mark_previous_passed([]) is False


def test_round_ends_at_defaults_one_hour():
    assert round_ends_at(AT, None) == AT + timedelta(hours=1)
    assert round_ends_at(AT, AT) == AT + timedelta(hours=1)
    assert round_ends_at(AT, AT + timedelta(minutes=45)) == AT + timedelta(minutes=45)


def test_sync_top_mirrors_round_fields():
    caller = InterviewCaller(enabled=True, callerName="Sam")
    tip = _round("scheduled", stage="panel", interviewerName="Dana", caller=caller, endsAt=AT + timedelta(minutes=30))
    iv = SimpleNamespace(
        stage=None, status=None, scheduledAt=AT, endsAt=None,
        interviewerName=None, caller=None, stageHistory=[tip],
    )
    _sync_top_from_tip(iv)
    assert iv.interviewerName == "Dana"
    assert iv.caller == caller
    assert iv.endsAt == AT + timedelta(minutes=30)


def test_sync_top_follows_timed_tip_within_same_day():
    tip = _round("scheduled", stage="panel", endsAt=AT + timedelta(hours=6))
    tip.scheduledAt = AT + timedelta(hours=5)  # 19:00, same UTC day as the interview's 14:00
    iv = SimpleNamespace(
        stage=None, status=None, scheduledAt=AT, endsAt=AT + timedelta(hours=1),
        interviewerName=None, caller=None, stageHistory=[tip],
    )
    _sync_top_from_tip(iv)
    assert iv.scheduledAt == AT + timedelta(hours=5)
    assert iv.endsAt == AT + timedelta(hours=6)


def test_coworker_on_earlier_round_can_still_view():
    from bson import ObjectId

    from app.routers.interviews import _can_view
    mate, owner = ObjectId(), ObjectId()
    earlier = _round("passed", caller=InterviewCaller(enabled=True, coworkerIds=[mate]))
    later = _round("scheduled", stage="panel")
    iv = SimpleNamespace(createdBy=owner, caller=None, stageHistory=[earlier, later])
    assert _can_view(iv, SimpleNamespace(id=mate, role="staff")) is True
    assert _can_view(iv, SimpleNamespace(id=ObjectId(), role="staff")) is False
