"""Pure unit tests for caller mode: payload validation, time math and channel text.

No database needed: `pytest --noconftest tests/test_caller.py`.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from beanie import PydanticObjectId

from app.interview_caller import (
    CallerError,
    CallerIn,
    build_round_caller,
    round_caller_snapshot,
)
from app.models.interview import InterviewCaller, InterviewStageEvent
from app.slack_bot import (
    caller_round_text,
    fmt_caller_time,
    slack_time,
)

OWNER = PydanticObjectId()
B = PydanticObjectId()
START = datetime(2026, 9, 29, 14, 30, tzinfo=timezone.utc)


def _build(previous=None, **kw):
    return build_round_caller(CallerIn(enabled=True, **kw), starts_at=START, previous=previous)


def _entry(caller):
    return InterviewStageEvent(stage="intro", at=START, scheduledAt=START, status="scheduled", caller=caller)


def test_defaults_to_tbd():
    c = _build()
    assert c.enabled is True
    assert c.callerName == "TBD"
    assert c.startsAt == START  # the caller joins at the round's start
    assert c.timezone is None  # Slack shows each reader their own time


def test_old_clients_may_still_send_a_timezone():
    assert _build(timezone="Mars/Olympus").timezone is None


def test_rejects_bad_method():
    with pytest.raises(CallerError):
        _build(method="carrier_pigeon")


def test_old_clients_may_still_send_coworker_ids():
    c = _build(coworkerIds=[str(B)])
    assert c.enabled is True
    assert not hasattr(c, "coworkerIds")


def test_disable_keeps_previous_data_but_turns_off():
    prev = _build(callerName="Sam")
    prev.slackChannelTs = "123.456"
    off = build_round_caller(CallerIn(enabled=False), starts_at=START, previous=prev)
    assert off.enabled is False
    assert off.callerName == "Sam"
    assert round_caller_snapshot(_entry(off)) is None


def test_update_keeps_slack_thread_ts():
    prev = _build()
    prev.slackChannelTs = "111.222"
    nxt = _build(previous=prev, callerName="Sam")
    assert nxt.slackChannelTs == "111.222"
    assert round_caller_snapshot(_entry(nxt)) != round_caller_snapshot(_entry(prev))


def test_naive_start_from_mongo_is_tagged_utc():
    c = InterviewCaller(enabled=True, startsAt=datetime(2026, 9, 29, 14, 30))
    assert c.startsAt.tzinfo == timezone.utc


def test_fmt_caller_time():
    tz = ZoneInfo("America/New_York")
    start = datetime(2026, 9, 29, 14, 30, tzinfo=timezone.utc)
    assert fmt_caller_time(None, tz) == "TBD"
    assert fmt_caller_time(start, tz) == "Tue 9/29 10:30 AM EDT (GMT−4:00)"
    assert fmt_caller_time(start, tz, with_day=False) == "10:30 AM EDT"


def test_slack_time_shows_each_reader_their_own_time():
    ts = int(START.timestamp())
    assert slack_time(None) == "TBD"
    assert slack_time(START) == (
        f"<!date^{ts}^{{date_short_pretty}} {{time}}|Tue 9/29 10:30 AM EDT (GMT−4:00)>"
    )
    assert slack_time(START, with_day=False) == f"<!date^{ts}^{{time}}|10:30 AM EDT>"
    # Naive datetimes from Mongo are UTC.
    assert slack_time(START.replace(tzinfo=None)) == slack_time(START)


def _iv(owner, *, caller=None, company="Acme"):
    return SimpleNamespace(
        id=PydanticObjectId(),
        createdBy=owner,
        accountId=None,
        companyName=company,
        jobUrl=None,
        interviewerName="Natalie Ross",
        stage="intro",
        caller=caller,
    )


def _user(name, slack=None):
    return SimpleNamespace(name=name, email=f"{name}@x.com", slackUserId=slack)


def test_caller_round_text_with_and_without_details():
    account = SimpleNamespace(name="Orion Osborn", country="US")
    iv = _iv(OWNER, company="Wikimedia")
    iv.jobUrl = "https://job-boards.greenhouse.io/wikimedia/jobs/7919287"
    entry = SimpleNamespace(
        stage="intro",
        scheduledAt=START,
        status="scheduled",
        interviewerName="Natalie Ross",
        caller=InterviewCaller(
            enabled=True,
            startsAt=START,
            method="phone_hushed",
            methodValue="+1 555 0100",
        ),
    )
    text = caller_round_text(iv, entry, account=account, owner=_user("A", "UA"))
    assert text.startswith(":telephone_receiver: <@UA> — *Caller needed*")
    assert "*Profile:* Orion Osborn" in text
    assert "*Company name:* Wikimedia" in text
    assert "*JD Link:* https://job-boards.greenhouse.io/wikimedia/jobs/7919287" in text
    assert "*Recruiter name:* Natalie Ross 🇺🇸" in text
    assert "*Stage:* Intro" in text
    assert f"*Interview time:* {slack_time(START)}" in text
    assert "*Method:* Phone (Hushed) — +1 555 0100" in text
    assert "*Caller assigned:* TBD" in text
    assert "*Coworkers:*" not in text and "*Interview manager:*" not in text
    assert "cc " not in text  # no interview managers to cc

    entry.interviewerName = None
    entry.caller = InterviewCaller(enabled=True)
    text = caller_round_text(iv, entry, account=account, owner=_user("A"))
    assert "*Recruiter name:* —" in text
    assert "*Method:* TBD" in text
    assert "Canceled" not in text


def test_caller_round_text_marks_canceled_rounds():
    iv = _iv(OWNER, company="Wikimedia")
    entry = SimpleNamespace(
        stage="intro", scheduledAt=START, status="canceled", interviewerName=None,
        caller=InterviewCaller(enabled=True, startsAt=START),
    )
    for status in ("canceled", "rejected"):
        entry.status = status
        text = caller_round_text(iv, entry, account=None, owner=_user("A", "UA"))
        assert text.startswith(":x: <@UA> — *Canceled* ~Caller needed~")
        assert f"*Interview time:* ~{slack_time(START)}~" in text


def test_caller_round_text_without_a_caller_needs_none():
    iv = _iv(OWNER, company="Wikimedia")
    entry = SimpleNamespace(
        stage="intro", scheduledAt=START, status="scheduled", interviewerName=None,
        caller=InterviewCaller(enabled=False, startsAt=START),
    )
    text = caller_round_text(iv, entry, account=None, owner=_user("A", "UA"))
    assert text.splitlines()[0] == ":telephone_receiver: <@UA> — *No caller needed* for Intro"
    assert f"*Interview time:* {slack_time(START)}" in text
