"""Which thread event a change is, and the owner mention (pure, no Slack, no DB)."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.interview_caller import caller_thread_event, round_caller_snapshot  # noqa: E402
from app.models.interview import InterviewCaller, InterviewStageEvent  # noqa: E402
from app.slack_bot import _mention  # noqa: E402

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
LATER = START + timedelta(days=1)


def entry(*, at=START, status="scheduled", confirmed=False, caller=True, stage="intro", name="Sam"):
    return InterviewStageEvent(
        stage=stage, at=at, scheduledAt=at, status=status, confirmed=confirmed,
        caller=InterviewCaller(enabled=caller, callerName=name, startsAt=at, method="video",
                               methodValue="https://meet.example/x"),
    )


def snap(**kw):
    return round_caller_snapshot(entry(**kw))


def user(name, slack=None):
    return SimpleNamespace(name=name, email=f"{name.lower()}@x.io", slackUserId=slack)


# --- caller_thread_event ------------------------------------------------------------------------


def test_event_moved_wins_when_added():
    assert caller_thread_event(None, snap(), added=True) == "moved"
    assert caller_thread_event(None, None, added=True) == "moved"  # the new round has no caller


def test_event_removed():
    assert caller_thread_event(snap(), None, removed=True) == "removed"


def test_event_ended_and_back_on():
    assert caller_thread_event(snap(), snap(status="canceled")) == "ended"
    assert caller_thread_event(snap(), snap(status="rejected")) == "ended"
    assert caller_thread_event(snap(status="canceled"), snap()) == "back_on"


def test_event_rescheduled_before_confirmed():
    assert caller_thread_event(snap(confirmed=True), snap(at=LATER)) == "rescheduled"
    assert caller_thread_event(snap(), snap(at=LATER, confirmed=True)) == "rescheduled"


def test_event_confirmed_and_unconfirmed():
    assert caller_thread_event(snap(), snap(confirmed=True)) == "confirmed"
    assert caller_thread_event(snap(confirmed=True), snap()) == "unconfirmed"


def test_event_details_for_other_fields():
    assert caller_thread_event(snap(name="TBD"), snap(name="Dana")) == "details"


def test_event_none_when_unchanged_or_caller_toggled():
    assert caller_thread_event(snap(), snap()) is None
    assert caller_thread_event(snap(), None) is None  # caller turned off: the main post says so
    assert caller_thread_event(None, snap()) is None


def test_mention_without_slack_id_is_escaped_so_a_name_cannot_ping():
    assert _mention(user("<!channel> & co")) == "&lt;!channel&gt; &amp; co"
    assert _mention(user("Maria")) == "Maria"
    assert _mention(user("Ana", "U0ANA00001")) == "<@U0ANA00001>"
    assert _mention(SimpleNamespace(name="", email="<!here>@x.io", slackUserId=None)) == "&lt;!here&gt;@x.io"
