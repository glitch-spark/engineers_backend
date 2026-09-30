"""Pure unit tests for caller mode: payload validation, time math, digest and channel text.

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
    is_coworker,
    round_caller_snapshot,
)
from app.models.interview import InterviewCaller, InterviewStageEvent
from app.slack_bot import (
    caller_round_text,
    digest_blocks,
    digest_summary,
    fmt_caller_time,
    split_digest_interviews,
)

OWNER = PydanticObjectId()
B = PydanticObjectId()
C = PydanticObjectId()
START = datetime(2026, 9, 29, 14, 30, tzinfo=timezone.utc)


def _build(previous=None, valid=None, **kw):
    body = CallerIn(enabled=True, **kw)
    return build_round_caller(
        body,
        starts_at=START,
        owner_id=OWNER,
        valid_coworker_ids={str(B), str(C)} if valid is None else valid,
        default_tz="America/New_York",
        previous=previous,
    )


def _entry(caller):
    return InterviewStageEvent(stage="intro", at=START, scheduledAt=START, status="scheduled", caller=caller)


def test_defaults_to_tbd_and_owner_timezone():
    c = _build()
    assert c.enabled is True
    assert c.callerName == "TBD"
    assert c.startsAt == START  # the caller joins at the round's start
    assert c.timezone == "America/New_York"
    assert c.coworkerIds == []


def test_rejects_bad_timezone_method_time_and_unknown_coworker():
    with pytest.raises(CallerError):
        _build(timezone="Mars/Olympus")
    with pytest.raises(CallerError):
        _build(method="carrier_pigeon")
    with pytest.raises(CallerError):
        _build(coworkerIds=[str(PydanticObjectId())])


def test_coworkers_deduped_and_owner_dropped():
    c = _build(coworkerIds=[str(B), str(B), str(OWNER), str(C)])
    assert c.coworkerIds == [B, C]
    assert is_coworker(c, B)
    assert not is_coworker(c, OWNER)


def test_disable_keeps_previous_data_but_turns_off():
    prev = _build(callerName="Sam", coworkerIds=[str(B)])
    prev.slackChannelTs = "123.456"
    off = build_round_caller(
        CallerIn(enabled=False),
        starts_at=START,
        owner_id=OWNER,
        valid_coworker_ids=set(),
        default_tz=None,
        previous=prev,
    )
    assert off.enabled is False
    assert off.callerName == "Sam"
    assert round_caller_snapshot(_entry(off)) is None
    assert not is_coworker(off, B)


def test_update_keeps_slack_thread_ts():
    prev = _build()
    prev.slackChannelTs = "111.222"
    nxt = _build(previous=prev, valid=set(), callerName="Sam")
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


def test_split_digest_mine_first_and_sorted_by_time():
    early = InterviewCaller(enabled=True, startsAt=datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc))
    late = InterviewCaller(enabled=True, startsAt=datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc))
    own_tbd = _iv(B, company="OwnTBD")
    own_late = _iv(B, caller=late, company="OwnLate")
    joined = _iv(OWNER, caller=early, company="Joined")
    mine, with_caller = split_digest_interviews([own_tbd, joined, own_late, joined], B)
    assert [iv.companyName for iv in mine] == ["OwnLate", "OwnTBD"]
    assert [iv.companyName for iv in with_caller] == ["Joined"]


def test_digest_summary_and_numbering_across_sections():
    assert digest_summary(2, 0) == (
        "You have 2 interviews today. Enjoy your interviews, and good luck! :four_leaf_clover:"
    )
    assert digest_summary(2, 2) == (
        "You have 4 interviews today (2 yours, 2 as caller). "
        "Enjoy your interviews, and good luck! :four_leaf_clover:"
    )
    assert digest_summary(1, 0).startswith("You have 1 interview today. Enjoy your interview,")

    joined = _iv(
        OWNER,
        caller=InterviewCaller(enabled=True, method="video", methodValue="https://meet.example/x"),
    )
    blocks = digest_blocks(
        [_iv(B), _iv(B)],
        [joined],
        profiles={},
        hosts={str(OWNER): "<@UOWNER>"},
        tz=ZoneInfo("America/New_York"),
    )
    texts = "\n".join(b["text"]["text"] for b in blocks if b.get("type") == "section")
    assert "*My interviews (2)*" in texts
    assert "*Calls with you as caller (1)*" in texts
    assert "\n3. *Time TBD*" in texts
    assert "host <@UOWNER>" in texts
    assert "Video meeting" in texts


def test_digest_omits_empty_section():
    blocks = digest_blocks([_iv(B)], [], profiles={}, hosts={}, tz=ZoneInfo("UTC"))
    texts = "\n".join(b["text"]["text"] for b in blocks if b.get("type") == "section")
    assert "Calls with you as caller" not in texts


def _user(name, slack=None):
    return SimpleNamespace(name=name, email=f"{name}@x.com", slackUserId=slack)


def test_caller_round_text_with_and_without_details():
    account = SimpleNamespace(name="Orion Osborn", country="US")
    iv = _iv(OWNER, company="Wikimedia")
    iv.jobUrl = "https://job-boards.greenhouse.io/wikimedia/jobs/7919287"
    entry = SimpleNamespace(
        stage="intro",
        scheduledAt=datetime(2026, 9, 29, 14, 30, tzinfo=timezone.utc),
        interviewerName="Natalie Ross",
        caller=InterviewCaller(
            enabled=True,
            startsAt=datetime(2026, 9, 29, 14, 30, tzinfo=timezone.utc),
            timezone="America/New_York",
            method="phone_hushed",
            methodValue="+1 555 0100",
        ),
    )
    text = caller_round_text(
        iv,
        entry,
        account=account,
        owner=_user("A", "UA"),
        coworkers=[_user("B", "UB"), _user("Casey")],
    )
    assert text.startswith(":telephone_receiver: *Caller needed* — posted by <@UA>")
    assert "*Profile:* Orion Osborn" in text
    assert "*Company name:* Wikimedia" in text
    assert "*JD Link:* https://job-boards.greenhouse.io/wikimedia/jobs/7919287" in text
    assert "*Recruiter name:* Natalie Ross 🇺🇸" in text
    assert "*Stage:* Intro" in text
    assert "*Interview time:* Tue 9/29 10:30 AM EDT (GMT−4:00)" in text
    assert "*Method:* Phone (Hushed) — +1 555 0100" in text
    assert "*Caller assigned:* TBD" in text
    assert "*Coworkers:* <@UB> Casey" in text

    entry.interviewerName = None
    entry.caller = InterviewCaller(enabled=True, timezone="America/New_York")
    text = caller_round_text(iv, entry, account=account, owner=_user("A"), coworkers=[])
    assert "*Recruiter name:* —" in text
    assert "*Method:* TBD" in text
    assert "*Coworkers:*" not in text
