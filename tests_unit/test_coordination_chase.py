"""The release cutoff, the after-interview check and the 7-day result reminder, for caller and self rounds, posted in
each round's home thread in the daily channel (Slack faked: `slack_bot._slack_api` is a
recorder, `get_settings` is patched, no test reads a .env or sets the cutoff through the environment)."""

import asyncio
import os
from datetime import datetime, time, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import slack_bot  # noqa: E402
from app.config import Settings  # noqa: E402
from app.coordination import chase  # noqa: E402
from app.models import interview as interview_model  # noqa: E402
from app.models import Account, Interview, ResultRoundupPost, SlackAlertLog, UserExtra  # noqa: E402
from app.models.interview import InterviewCaller, InterviewStageEvent, SlackThread  # noqa: E402
from app.models.round_slack_post import RoundSlackPost  # noqa: E402
from app.interview_day import home  # noqa: E402

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

UTC = timezone.utc
START = datetime(2026, 11, 16, 18, 30, tzinfo=UTC)
END = datetime(2026, 11, 16, 19, 30, tzinfo=UTC)
CUTOFF = "2026-11-02T00:00"
DAILY = ("C_DAILY", time(5, 0), chase.CHASE_TZ)


def utc(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm, tzinfo=UTC)


def settings(**over):
    values = dict(SLACK_BOT_TOKEN="x", SLACK_CALLER_CHANNEL_ID="C1", SLACK_CALLER_ASKS_FROM=CUTOFF)
    values.update(over)
    return SimpleNamespace(**values)


class Slack:
    """Records every Slack call; `errors` is a queue of errors, one per chat.postMessage call (None = succeed)."""

    def __init__(self):
        self.calls, self.errors, self._n = [], [], count(1)

    async def __call__(self, method, body):
        self.calls.append((method, body))
        if self.errors and (err := self.errors.pop(0)):
            raise RuntimeError(err)
        return {"ts": f"9.{next(self._n)}"}

    def posts(self):
        return [b for m, b in self.calls if m == "chat.postMessage"]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["coord-chase-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, SlackAlertLog, ResultRoundupPost,
                                                         RoundSlackPost])
    return database


@pytest.fixture
def slack(monkeypatch, db):
    fake = Slack()
    monkeypatch.setattr(slack_bot, "_slack_api", fake)
    monkeypatch.setattr(chase, "get_settings", lambda: settings())
    monkeypatch.setattr(slack_bot, "_caller_daily_configured", lambda: DAILY)
    return fake


@pytest.fixture
async def justin(db):
    return await UserExtra(name="Justin", email="justin@x.io", role="staff", slackUserId="U0JUSTIN01").insert()


async def add(owner, *, start=START, end=END, status="scheduled", caller=True, stage="intro", thread=True,
              company="Acme") -> Interview:
    entry = InterviewStageEvent(
        stage=stage, at=start, scheduledAt=start, endsAt=end, status=status,
        caller=InterviewCaller(enabled=caller, callerName="Sam", startsAt=start, method="video") if caller else None,
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=start, companyName=company,
        stageHistory=[entry.model_dump()], stage=stage, status=status,
        slackThread=SlackThread(channel="C1", ts="100.1") if thread else None,
    ).insert()


def action_ids(post):
    return [e["action_id"] for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]]


def labels(post):
    return [e["text"]["text"] for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]]


async def follow_up(iv, at, index=-1) -> Interview:
    iv.stageHistory[index].followUps.append({"at": at, "by": iv.createdBy})
    await iv.save()
    return iv


def values(post):
    return [e["value"] for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]]


# --- the setting -------------------------------------------------------------------------------------


def test_asks_from_parses_and_validates(monkeypatch):
    monkeypatch.setattr(chase, "get_settings", lambda: settings(SLACK_CALLER_ASKS_FROM="2026-11-23T00:00"))
    assert chase.asks_from() == datetime(2026, 11, 23, 6, 0, tzinfo=UTC)  # CST, UTC-6
    monkeypatch.setattr(chase, "get_settings", lambda: settings(SLACK_CALLER_ASKS_FROM="2026-08-01T00:00"))
    assert chase.asks_from() == datetime(2026, 8, 1, 5, 0, tzinfo=UTC)  # CDT, UTC-5
    monkeypatch.setattr(chase, "get_settings", lambda: settings(SLACK_CALLER_ASKS_FROM=None))
    assert chase.asks_from() is None

    base = dict(_env_file=None, MONGODB_URI="m", AUTH_SECRET="a" * 32)
    assert Settings(**base, SLACK_CALLER_ASKS_FROM="2026-11-23T00:00").SLACK_CALLER_ASKS_FROM == "2026-11-23T00:00"
    assert Settings(**base, SLACK_CALLER_ASKS_FROM="  ").SLACK_CALLER_ASKS_FROM is None
    assert Settings(**base).SLACK_CALLER_ASKS_FROM is None
    for bad in ("2026-11-23", "2026-13-01T00:00", "2026-02-30T00:00"):
        with pytest.raises(ValidationError) as err:
            Settings(**base, SLACK_CALLER_ASKS_FROM=bad)
        assert "SLACK_CALLER_ASKS_FROM" in str(err.value)
        assert "must be YYYY-MM-DDTHH:MM" in str(err.value)


# --- the after-call ask ------------------------------------------------------------------------------


async def test_asks_off_without_the_setting(slack, justin, monkeypatch):
    await add(justin)
    monkeypatch.setattr(chase, "get_settings", lambda: settings(SLACK_CALLER_ASKS_FROM=None))
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 0, "failed": 0}
    assert await chase.run_result_reminders(utc(30, 19, 31)) == {"sent": 0, "failed": 0}
    assert slack.calls == []
    assert await SlackAlertLog.find_all().to_list() == []


async def test_asks_off_without_the_token(slack, justin, monkeypatch):
    await add(justin)
    monkeypatch.setattr(chase, "get_settings", lambda: settings(SLACK_BOT_TOKEN=""))
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 0, "failed": 0}
    assert slack.calls == []


async def test_off_without_the_daily_channel(slack, justin, monkeypatch):
    await add(justin)
    monkeypatch.setattr(slack_bot, "_caller_daily_configured", lambda: None)
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 0, "failed": 0}
    assert await chase.run_result_reminders(utc(23, 19, 31)) == {"sent": 0, "failed": 0}
    assert slack.calls == []


async def test_runs_without_the_caller_channel(slack, justin, monkeypatch):
    await add(justin)
    monkeypatch.setattr(chase, "get_settings", lambda: settings(SLACK_CALLER_CHANNEL_ID=None))
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 1, "failed": 0}


async def test_after_call_ask_once_after_the_end(slack, justin):
    iv = await add(justin)
    rid = iv.stageHistory[0].id

    assert await chase.run_after_call_asks(utc(16, 19, 44)) == {"sent": 0, "failed": 0}  # 14 min after the end
    assert slack.posts() == []

    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 1, "failed": 0}
    [post] = slack.posts()
    assert post["channel"] == "C_DAILY" and "thread_ts" not in post  # no home yet: the ask becomes it
    assert action_ids(post) == ["coord_after_completed", "coord_after_passed", "coord_after_rejected",
                                "coord_after_rescheduled", "coord_after_canceled"]
    labels = [e["text"]["text"] for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert labels == ["Completed", "Passed", "Rejected", "Rescheduled…", "Canceled"]
    assert values(post) == [f"{iv.id}:{rid}"] * 5
    assert post["text"].startswith("<@U0JUSTIN01> — Acme Intro ended at <!date^")
    assert post["text"].endswith(". How did it go?")

    assert await chase.run_after_call_asks(utc(16, 19, 51)) == {"sent": 0, "failed": 0}
    assert len(slack.posts()) == 1
    rows = await SlackAlertLog.find_all().to_list()
    assert [r.kind for r in rows] == [f"after-call:{rid}:{int(START.timestamp())}"]


async def test_already_sent_rounds_cost_no_insert(slack, justin, monkeypatch):
    await add(justin)
    await chase.run_after_call_asks(utc(16, 19, 46))
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(slack_bot, "_reminder_claim", claim)
    assert await chase.run_after_call_asks(utc(16, 19, 51)) == {"sent": 0, "failed": 0}
    claim.assert_not_awaited()


async def test_no_end_means_one_hour(slack, justin):
    await add(justin, end=None)  # ends at start + 1 h = 19:30Z
    assert await chase.run_after_call_asks(utc(16, 19, 44)) == {"sent": 0, "failed": 0}
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 1, "failed": 0}
    assert len(slack.posts()) == 1


async def test_ask_threads_under_the_rounds_home(slack, justin):
    iv = await add(justin)
    await home.record_home(iv.stageHistory[0].id, str(iv.id), START, "C_DAILY", "42.1")
    await chase.run_after_call_asks(utc(16, 19, 46))
    [post] = slack.posts()
    assert post["channel"] == "C_DAILY" and post["thread_ts"] == "42.1"


async def test_round_without_a_home_gets_one_from_its_ask(slack, justin):
    iv = await add(justin)
    await chase.run_after_call_asks(utc(16, 19, 46))
    (h,) = await RoundSlackPost.find_all().to_list()
    assert (h.roundId, h.startEpoch, h.ts) == (iv.stageHistory[0].id, int(START.timestamp()), "9.1")


@pytest.mark.parametrize("kwargs", [dict(caller=False, thread=False), dict(thread=False)],
                         ids=["self-round", "caller-round-without-a-post"])
async def test_self_and_unposted_rounds_are_asked(slack, justin, kwargs):
    await add(justin, **kwargs)
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 1, "failed": 0}


async def test_rounds_ended_over_two_days_ago_are_not_asked(slack, justin):
    await add(justin)  # ended Nov 16 19:30Z
    assert await chase.run_after_call_asks(utc(18, 19, 31)) == {"sent": 0, "failed": 0}
    assert await chase.run_after_call_asks(utc(18, 19, 29)) == {"sent": 1, "failed": 0}


async def test_round_asked_under_the_old_flow_is_not_asked_again(slack, justin):
    iv = await add(justin)
    await slack_bot._reminder_claim(f"after-call:{iv.stageHistory[0].id}:{int(START.timestamp())}")
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 0, "failed": 0}
    assert slack.posts() == []


async def test_a_round_stored_without_a_status_is_asked(slack, justin, db, monkeypatch):
    iv = await add(justin)
    raw = iv.model_dump()["stageHistory"]
    raw[0]["status"] = None
    await Interview.find_one(Interview.id == iv.id).update({"$set": {"stageHistory": raw}})
    assert (await db["interviews"].find_one({}))["stageHistory"][0]["status"] is None  # stored without a status
    # Loading resolves a missing tip status from the clock: a round still ahead reads as scheduled (one already past
    # reads as completed). Hold the clock before the round, so the stored None is the one that must be found.
    real = interview_model.resolve_interview
    monkeypatch.setattr(interview_model, "resolve_interview",
                        lambda doc, *, now: real(doc, now=datetime(2026, 11, 1, tzinfo=UTC)))
    assert (await Interview.get(iv.id)).stageHistory[0].status.value == "scheduled"
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 1, "failed": 0}


async def test_a_legacy_rescheduled_round_is_asked(slack, justin):
    await add(justin, status="rescheduled")
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 1, "failed": 0}


@pytest.mark.parametrize("kwargs", [
    pytest.param(dict(status="canceled"), id="canceled"),
    pytest.param(dict(status="passed"), id="passed"),
    pytest.param(dict(start=datetime(2026, 11, 1, 19, 0, tzinfo=UTC), end=datetime(2026, 11, 1, 20, 0, tzinfo=UTC)),
                 id="before-the-cutoff"),
    pytest.param(dict(stage="home_assessment"), id="home-assessment"),
])
async def test_not_asked(slack, justin, kwargs):
    await add(justin, **kwargs)
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 0, "failed": 0}
    assert slack.posts() == []
    assert await SlackAlertLog.find_all().to_list() == []


async def test_a_round_marked_completed_before_the_check_is_asked_for_its_result(slack, justin):
    """Marked completed early with no result: the check still asks, with the result buttons only."""
    iv = await add(justin, status="completed")
    rid = iv.stageHistory[0].id
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 1, "failed": 0}
    [post] = slack.posts()
    assert action_ids(post) == ["coord_result_passed", "coord_result_rejected"]
    assert values(post) == [f"{iv.id}:{rid}"] * 2
    assert post["text"].endswith("has no result yet. How did it end?")
    assert await chase.run_after_call_asks(utc(16, 19, 51)) == {"sent": 0, "failed": 0}
    assert [r.kind for r in await SlackAlertLog.find_all().to_list()] == [f"after-call:{rid}:{int(START.timestamp())}"]


async def test_caller_turned_off_is_still_asked(slack, justin):
    iv = await add(justin)
    raw = iv.model_dump()["stageHistory"]
    raw[0]["caller"]["enabled"] = False
    await Interview.find_one(Interview.id == iv.id).update({"$set": {"stageHistory": raw}})
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 1, "failed": 0}


async def test_rescheduled_round_is_asked_again(slack, justin):
    iv = await add(justin)
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 1, "failed": 0}
    moved = utc(17, 18, 30)
    raw = iv.model_dump()["stageHistory"]
    raw[0]["scheduledAt"], raw[0]["endsAt"] = moved, utc(17, 19, 30)
    await Interview.find_one(Interview.id == iv.id).update({"$set": {"stageHistory": raw}})

    assert await chase.run_after_call_asks(utc(17, 19, 44)) == {"sent": 0, "failed": 0}
    assert await chase.run_after_call_asks(utc(17, 19, 46)) == {"sent": 1, "failed": 0}
    assert len(slack.posts()) == 2
    kinds = sorted(r.kind for r in await SlackAlertLog.find_all().to_list())
    rid = iv.stageHistory[0].id
    assert kinds == sorted([f"after-call:{rid}:{int(START.timestamp())}", f"after-call:{rid}:{int(moved.timestamp())}"])


async def test_failed_post_releases_the_claim(slack, justin):
    await add(justin)
    slack.errors = ["ratelimited"]
    assert await chase.run_after_call_asks(utc(16, 19, 46)) == {"sent": 0, "failed": 1}
    assert await SlackAlertLog.find_all().to_list() == []
    assert await chase.run_after_call_asks(utc(16, 19, 51)) == {"sent": 1, "failed": 0}
    assert len(await SlackAlertLog.find_all().to_list()) == 1


async def test_an_unlinked_owner_is_named_in_plain_text(slack, db):
    owner = await UserExtra(name="Ana", email="ana@x.io", role="staff").insert()
    await add(owner)
    await chase.run_after_call_asks(utc(16, 19, 46))
    assert slack.posts()[0]["text"].startswith("Ana — Acme Intro ended at ")


# --- the 7-day result reminder -----------------------------------------------------------------------


async def test_result_reminder_seven_days_after(slack, justin):
    iv = await add(justin, status="completed")
    rid = iv.stageHistory[0].id

    assert await chase.run_result_reminders(utc(23, 19, 29)) == {"sent": 0, "failed": 0}
    assert slack.posts() == []
    assert await chase.run_result_reminders(utc(23, 19, 31)) == {"sent": 1, "failed": 0}
    [post] = slack.posts()
    assert post["channel"] == "C_DAILY" and "thread_ts" not in post
    assert action_ids(post) == ["coord_followed_up", "coord_result_passed", "coord_result_rejected"]
    assert labels(post) == ["I followed up", "Passed", "Rejected"]
    assert values(post) == [f"{iv.id}:{rid}"] * 3
    assert post["text"].startswith("<@U0JUSTIN01> — Acme Intro on <!date^")
    assert post["text"].endswith(" has no result yet. Follow up with the company, then mark it here.")
    assert post["blocks"][0]["text"]["text"] == post["text"]
    assert await chase.run_result_reminders(utc(23, 19, 36)) == {"sent": 0, "failed": 0}
    assert len(slack.posts()) == 1
    assert [r.kind for r in await SlackAlertLog.find_all().to_list()] == [f"result-reminder:{rid}"]


async def test_a_round_stored_without_a_status_gets_the_reminder(slack, justin, db):
    iv = await add(justin)
    raw = iv.model_dump()["stageHistory"]
    raw[0]["status"] = None
    await Interview.find_one(Interview.id == iv.id).update({"$set": {"stageHistory": raw}})
    assert (await db["interviews"].find_one({}))["stageHistory"][0]["status"] is None  # stored without a status
    # Its time is past, so loading reads it as completed (app/interview_normalize.py).
    assert (await Interview.get(iv.id)).stageHistory[0].status.value == "completed"
    assert await chase.run_result_reminders(utc(23, 19, 29)) == {"sent": 0, "failed": 0}
    assert await chase.run_result_reminders(utc(23, 19, 31)) == {"sent": 1, "failed": 0}
    assert action_ids(slack.posts()[0]) == ["coord_followed_up", "coord_result_passed", "coord_result_rejected"]


async def test_result_reminder_covers_self_rounds_in_the_home_thread(slack, justin):
    iv = await add(justin, status="completed", caller=False, thread=False)
    await home.record_home(iv.stageHistory[0].id, str(iv.id), START, "C_DAILY", "42.1")
    assert await chase.run_result_reminders(utc(23, 19, 31)) == {"sent": 1, "failed": 0}
    assert slack.posts()[0]["thread_ts"] == "42.1"


async def test_result_reminder_skips_rounds_past_the_two_day_window(slack, justin):
    await add(justin, status="completed")  # ended Nov 16 19:30Z: its 7-day mark is Nov 23 19:30Z
    assert await chase.run_result_reminders(utc(25, 19, 31)) == {"sent": 0, "failed": 0}
    assert await chase.run_result_reminders(utc(25, 19, 29)) == {"sent": 1, "failed": 0}


async def test_a_passed_round_gets_no_reminder(slack, justin):
    await add(justin, status="passed")
    assert await chase.run_result_reminders(utc(23, 19, 31)) == {"sent": 0, "failed": 0}
    assert slack.posts() == []


async def test_a_completed_round_ended_before_the_cutoff_gets_no_reminder(slack, justin):
    await add(justin, status="completed", start=datetime(2026, 11, 1, 19, 0, tzinfo=UTC),
              end=datetime(2026, 11, 1, 20, 0, tzinfo=UTC))
    assert await chase.run_result_reminders(utc(30, 19, 31)) == {"sent": 0, "failed": 0}


async def test_chase_query_is_bounded_below_by_the_cutoff(slack, justin, db, monkeypatch):
    """A round that ended before the lower bound is left out by the query itself, not only by the Python filter, and
    one the filter would accept is never left out."""
    cutoff = utc(2, 6)
    await add(justin, company="Old", start=utc(1, 19), end=utc(1, 20))  # ended before the cutoff
    await add(justin, company="OldNoEnd", start=utc(1, 19), end=None)  # start + 1 h, before it
    await add(justin, company="EdgeNoEnd", start=utc(2, 5, 30), end=None)  # ends 06:30, after it
    await add(justin, company="Long", start=utc(1, 22), end=utc(2, 8))  # starts early, ends after it
    await add(justin, company="Inverted", start=utc(2, 5, 30), end=utc(2, 5, 0))  # end <= start: +1 h
    await add(justin, company="Recent", start=utc(16, 18))

    queries = []
    real_find = Interview.find

    def spy(query, *a, **kw):
        queries.append(query)
        return real_find(query, *a, **kw)

    monkeypatch.setattr(Interview, "find", spy)
    rounds = await chase.ended_rounds("scheduled", ended_after=cutoff, ended_by=utc(20, 0))
    assert {c.interview.companyName for c in rounds} == {"EdgeNoEnd", "Long", "Inverted", "Recent"}

    [query] = queries
    found = {d["companyName"] async for d in db["interviews"].find(query)}
    assert found == {"EdgeNoEnd", "Long", "Inverted", "Recent"}  # the two that ended before it never load


async def test_failed_reminder_releases_the_claim(slack, justin):
    await add(justin, status="completed")
    slack.errors = ["ratelimited"]
    assert await chase.run_result_reminders(utc(23, 19, 31)) == {"sent": 0, "failed": 1}
    assert await SlackAlertLog.find_all().to_list() == []


async def test_a_round_already_followed_up_gets_no_follow_up_reminder(slack, justin):
    """Followed up in the app before the 7-day mark: step 2 has nothing left to ask."""
    await follow_up(await add(justin, status="completed"), utc(20, 12))
    assert await chase.run_result_reminders(utc(23, 19, 31)) == {"sent": 0, "failed": 0}
    assert slack.posts() == []


# --- "did you hear back?", 7 days after the follow-up ------------------------------------------------

FOLLOWED = utc(24, 12)  # its 7-day mark is Dec 1 12:00Z
BEFORE = datetime(2026, 12, 1, 11, 59, tzinfo=UTC)
DUE = datetime(2026, 12, 1, 12, 1, tzinfo=UTC)


async def test_heard_back_seven_days_after_the_follow_up_once(slack, justin):
    iv = await follow_up(await add(justin, status="completed"), FOLLOWED)
    rid = iv.stageHistory[0].id

    assert await chase.run_heard_back_asks(BEFORE) == {"sent": 0, "failed": 0}
    assert slack.posts() == []
    assert await chase.run_heard_back_asks(DUE) == {"sent": 1, "failed": 0}
    [post] = slack.posts()
    assert post["channel"] == "C_DAILY"
    assert post["text"].startswith("<@U0JUSTIN01> — Acme Intro on <!date^")
    assert post["text"].endswith(": did you hear back after your follow-up?")
    assert action_ids(post) == ["coord_result_no_answer", "coord_result_passed", "coord_result_rejected"]
    assert labels(post) == ["No, close as rejected", "Passed", "Rejected"]
    assert values(post) == [f"{iv.id}:{rid}"] * 3
    assert await chase.run_heard_back_asks(DUE + timedelta(minutes=5)) == {"sent": 0, "failed": 0}
    assert len(slack.posts()) == 1
    assert [r.kind for r in await SlackAlertLog.find_all().to_list()] == [
        f"heard-back:{rid}:{int(FOLLOWED.timestamp())}",
    ]


async def test_heard_back_is_asked_again_after_a_later_follow_up(slack, justin):
    iv = await follow_up(await add(justin, status="completed"), FOLLOWED)
    assert (await chase.run_heard_back_asks(DUE))["sent"] == 1
    second = datetime(2026, 12, 2, 12, 0, tzinfo=UTC)
    await follow_up(await Interview.get(iv.id), second)
    assert (await chase.run_heard_back_asks(second + timedelta(days=6)))["sent"] == 0
    assert (await chase.run_heard_back_asks(second + timedelta(days=7, minutes=1)))["sent"] == 1
    assert len(slack.posts()) == 2


async def test_heard_back_threads_under_the_rounds_home(slack, justin):
    iv = await follow_up(await add(justin, status="completed"), FOLLOWED)
    await home.record_home(iv.stageHistory[0].id, str(iv.id), START, "C_DAILY", "42.1")
    assert (await chase.run_heard_back_asks(DUE))["sent"] == 1
    assert slack.posts()[0]["thread_ts"] == "42.1"


async def test_heard_back_skips_follow_ups_past_the_two_day_window(slack, justin):
    await follow_up(await add(justin, status="completed"), FOLLOWED)
    assert await chase.run_heard_back_asks(DUE + timedelta(days=2)) == {"sent": 0, "failed": 0}
    assert slack.posts() == []


@pytest.mark.parametrize("status", ["passed", "rejected", "canceled", "scheduled"])
async def test_heard_back_is_not_asked_once_the_round_has_a_result(slack, justin, status):
    iv = await follow_up(await add(justin, status="completed"), FOLLOWED)
    iv.stageHistory[0].status = status
    await iv.save()
    assert await chase.run_heard_back_asks(DUE) == {"sent": 0, "failed": 0}
    assert slack.posts() == []
    assert await SlackAlertLog.find_all().to_list() == []


async def test_heard_back_is_only_for_the_latest_round(slack, justin):
    iv = await follow_up(await add(justin, status="completed"), FOLLOWED)
    later = InterviewStageEvent(stage="final", at=DUE, scheduledAt=DUE + timedelta(days=30), status="scheduled")
    iv.stageHistory.append(later)
    await iv.save()
    assert await chase.run_heard_back_asks(DUE) == {"sent": 0, "failed": 0}
    assert slack.posts() == []


async def test_heard_back_off_without_the_setting_and_for_rounds_before_the_cutoff(slack, justin, monkeypatch):
    await follow_up(await add(justin, status="completed", company="Old", start=datetime(2026, 11, 1, 19, 0, tzinfo=UTC),
                              end=datetime(2026, 11, 1, 20, 0, tzinfo=UTC)), FOLLOWED)
    assert await chase.run_heard_back_asks(DUE) == {"sent": 0, "failed": 0}
    await follow_up(await add(justin, status="completed"), FOLLOWED)
    monkeypatch.setattr(chase, "get_settings", lambda: settings(SLACK_CALLER_ASKS_FROM=None))
    assert await chase.run_heard_back_asks(DUE) == {"sent": 0, "failed": 0}
    assert slack.posts() == []


async def test_failed_heard_back_releases_the_claim(slack, justin):
    await follow_up(await add(justin, status="completed"), FOLLOWED)
    slack.errors = ["ratelimited"]
    assert await chase.run_heard_back_asks(DUE) == {"sent": 0, "failed": 1}
    assert await SlackAlertLog.find_all().to_list() == []
    assert await chase.run_heard_back_asks(DUE) == {"sent": 1, "failed": 0}


# --- the tick ----------------------------------------------------------------------------------------


async def test_chase_parts_are_isolated(slack, justin, monkeypatch):
    monkeypatch.setattr(chase, "run_after_call_asks", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(chase, "run_result_reminders", AsyncMock(return_value={"sent": 2, "failed": 0}))
    monkeypatch.setattr(chase, "run_heard_back_asks", AsyncMock(return_value={"sent": 3, "failed": 0}))
    assert await chase.run_coordination_chase(utc(16, 19, 31)) == {
        "after_call_asks": 0, "result_reminders": 2, "heard_back_asks": 3,
    }


async def test_roundup_is_no_longer_posted(slack, justin):
    await add(justin, status="completed")
    friday_evening = datetime(2026, 11, 20, 18, 5, tzinfo=chase.CHASE_TZ)
    await chase.run_coordination_chase(friday_evening)
    assert not hasattr(chase, "run_result_roundup")
    assert await ResultRoundupPost.find_all().to_list() == []


def _quiet_tick():
    return (
        patch("app.interview_day.post.run_day_post", AsyncMock(return_value="already_sent")),
        patch("app.interview_day.reminders.run_round_reminders", AsyncMock(return_value={"sent": 0, "failed": 0})),
        patch("app.interview_day.post.refresh_today", AsyncMock(return_value=False)),
        patch("app.standup.nudge.run_standup_nudge", AsyncMock(return_value="skipped")),
    )


def test_tick_reports_the_chase():
    a, b, c, d = _quiet_tick()
    run = AsyncMock(return_value={"after_call_asks": 2, "result_reminders": 1, "heard_back_asks": 3})
    with a, b, c, d, patch("app.coordination.chase.run_coordination_chase", run):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert out == {"caller_reminders": 0, "caller_daily": "already_sent", "standup_nudge": "skipped", "after_call_asks": 2, "result_reminders": 1,
                   "heard_back_asks": 3, "team_daily": {}, "team_reminders": 0}
    run.assert_awaited_once()
    assert run.await_args.args[0].tzinfo is not None


def test_tick_survives_a_chase_crash():
    a, b, c, d = _quiet_tick()
    with a, b, c, d, patch("app.coordination.chase.run_coordination_chase", AsyncMock(side_effect=RuntimeError("boom"))):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert out["after_call_asks"] == 0 and out["result_reminders"] == 0 and out["heard_back_asks"] == 0
    assert out["caller_daily"] == "already_sent"


def test_constants_and_answered_blocks():
    from app.coordination.blocks import answered_blocks

    assert answered_blocks("Done & dusted") == [{"type": "section", "text": {"type": "mrkdwn", "text": "Done & dusted"}}]
    assert len(answered_blocks("x" * 5000)[0]["text"]["text"]) <= 3000
    assert chase.RESULT_REMINDER_AFTER == timedelta(days=7)
    assert str(chase.CHASE_TZ) == "America/Chicago"


def test_tick_reports_team_daily_crash_as_a_dict():
    a, b, c, d = _quiet_tick()
    boom = AsyncMock(side_effect=RuntimeError("boom"))
    with a, b, c, d, patch("app.caller_teams.reminders.run_team_day_posts", boom), patch(
        "app.coordination.chase.run_coordination_chase", AsyncMock(return_value={})
    ):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert out["team_daily"] == {"_error": "failed"}
