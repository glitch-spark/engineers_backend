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
    build_caller,
    caller_snapshot,
    is_coworker,
    resync_caller_day,
)
from app.models.interview import InterviewCaller
from app.slack_bot import (
    caller_channel_text,
    digest_blocks,
    digest_summary,
    fmt_caller_time,
    split_digest_interviews,
)

OWNER = PydanticObjectId()
B = PydanticObjectId()
C = PydanticObjectId()
DAY = datetime(2026, 9, 29, 12, 0, 0)


def _build(**kw):
    body = CallerIn(enabled=True, **kw)
    return build_caller(
        body,
        scheduled_at=DAY,
        owner_id=OWNER,
        valid_coworker_ids={str(B), str(C)},
        default_tz="America/New_York",
    )


def test_defaults_to_tbd_and_owner_timezone():
    c = _build()
    assert c.enabled is True
    assert c.callerName == "TBD"
    assert c.startsAt is None
    assert c.timezone == "America/New_York"
    assert c.coworkerIds == []


def test_time_in_timezone_converts_to_utc():
    c = _build(time="10:30", timezone="America/New_York")
    assert c.startsAt == datetime(2026, 9, 29, 14, 30, tzinfo=timezone.utc)


def test_evening_new_york_lands_next_day_utc_but_keeps_local_day():
    c = _build(time="21:15", timezone="America/New_York")
    assert c.startsAt == datetime(2026, 9, 30, 1, 15, tzinfo=timezone.utc)
    local = c.startsAt.astimezone(ZoneInfo("America/New_York"))
    assert local.date().isoformat() == "2026-09-29"


def test_rejects_bad_timezone_method_time_and_unknown_coworker():
    with pytest.raises(CallerError):
        _build(timezone="Mars/Olympus")
    with pytest.raises(CallerError):
        _build(method="carrier_pigeon")
    with pytest.raises(CallerError):
        _build(time="25:99")
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
    off = build_caller(
        CallerIn(enabled=False),
        scheduled_at=DAY,
        owner_id=OWNER,
        valid_coworker_ids=set(),
        default_tz=None,
        previous=prev,
    )
    assert off.enabled is False
    assert off.callerName == "Sam"
    assert caller_snapshot(off) is None
    assert not is_coworker(off, B)


def test_update_keeps_slack_thread_ts():
    prev = _build()
    prev.slackChannelTs = "111.222"
    nxt = build_caller(
        CallerIn(enabled=True, callerName="Sam"),
        scheduled_at=DAY,
        owner_id=OWNER,
        valid_coworker_ids=set(),
        default_tz=None,
        previous=prev,
    )
    assert nxt.slackChannelTs == "111.222"
    assert caller_snapshot(nxt) != caller_snapshot(prev)


def test_resync_moves_start_to_new_day_keeping_local_time():
    c = _build(time="10:30", timezone="America/New_York")
    resync_caller_day(c, datetime(2026, 10, 2, 12, 0, 0))
    local = c.startsAt.astimezone(ZoneInfo("America/New_York"))
    assert (local.date().isoformat(), local.hour, local.minute) == ("2026-10-02", 10, 30)


def test_form_date_wins_over_utc_scheduled_at():
    # 9 PM New York on 9/29 is sent by the UI as scheduledAt 9/30 01:00 UTC.
    c = build_caller(
        CallerIn(enabled=True, date="2026-09-29", time="21:00", timezone="America/New_York"),
        scheduled_at=datetime(2026, 9, 30, 1, 0, 0),
        owner_id=OWNER,
        valid_coworker_ids=set(),
        default_tz=None,
    )
    assert c.startsAt == datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)
    with pytest.raises(CallerError):
        _build(date="29/09/2026")


def test_resync_is_noop_for_evening_call_stored_as_next_utc_day():
    c = _build(time="21:00", timezone="America/New_York")
    before = c.startsAt
    resync_caller_day(c, datetime(2026, 9, 30, 1, 0, 0))
    assert c.startsAt == before


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


def test_caller_channel_text_with_and_without_time():
    account = SimpleNamespace(name="Orion Osborn", country="US")
    iv = _iv(
        OWNER,
        company="Wikimedia",
        caller=InterviewCaller(
            enabled=True,
            startsAt=datetime(2026, 9, 29, 14, 30, tzinfo=timezone.utc),
            timezone="America/New_York",
            method="phone_hushed",
            methodValue="+1 555 0100",
        ),
    )
    iv.jobUrl = "https://job-boards.greenhouse.io/wikimedia/jobs/7919287"
    text = caller_channel_text(
        iv,
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

    iv.caller = InterviewCaller(enabled=True, timezone="America/New_York")
    text = caller_channel_text(iv, account=account, owner=_user("A"), coworkers=[], kind="updated")
    assert "*Interview time:* TBD" in text
    assert "*Method:* TBD" in text
    assert "*Caller details updated* by A" in text
    assert "*Coworkers:*" not in text
