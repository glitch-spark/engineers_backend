"""30-minute reminders: one reply per round in the day post's thread (Slack faked)."""

import os
from datetime import date, datetime, time, timedelta, timezone
from itertools import count
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import slack_bot
from app.interview_day import reminders
from app.models import Account, Interview, SlackAlertLog, UserExtra
from app.models.interview import InterviewCaller, InterviewStageEvent
from app.models.interview_day_post import InterviewDayPost

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
DAY = date(2026, 10, 7)
CONFIG = ("C_DAILY", time(5, 0), CHI)


def chi(hh, mm=0, ss=0):
    return datetime.combine(DAY, time(hh, mm, ss), tzinfo=CHI)


class Slack:
    """Records every Slack call; `errors["chat.postMessage"]` is a queue with one entry per call (string raises)."""

    def __init__(self):
        self.calls, self.errors, self._n = [], {}, count(1)

    async def __call__(self, method, body):
        self.calls.append((method, body))
        queue = self.errors.get(method)
        if queue and (err := queue.pop(0)):
            raise RuntimeError(err)
        return {"ts": f"9.{next(self._n)}"} if method == "chat.postMessage" else {}

    def posts(self):
        return [b for m, b in self.calls if m == "chat.postMessage"]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["reminders-unit"]
    await init_beanie(
        database=database,
        document_models=[Interview, UserExtra, Account, SlackAlertLog, InterviewDayPost],
    )
    return database


@pytest.fixture
def slack(monkeypatch, db):
    fake = Slack()
    monkeypatch.setattr(slack_bot, "_slack_api", fake)
    monkeypatch.setattr(slack_bot, "_caller_daily_configured", lambda: CONFIG)
    return fake


async def add(company, start, *, caller=True, status="scheduled", stage="intro"):
    at = start.astimezone(timezone.utc)
    entry = InterviewStageEvent(
        stage=stage, at=at, scheduledAt=at, status=status,
        caller=InterviewCaller(enabled=caller, callerName="Sam", startsAt=at, method="video") if caller else None,
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=PydanticObjectId(), scheduledAt=at, companyName=company,
        stageHistory=[entry.model_dump()], stage=stage, status=status,
    ).insert()


async def day_post():
    await InterviewDayPost(day=DAY.isoformat(), channel="C_DAILY", ts=["77.1", "77.2"], interviewIds=[]).insert()


async def test_self_and_caller_rounds_both_reminded(slack):
    await day_post()
    now = chi(9)
    await add("SelfCo", chi(9, 20), caller=False)
    await add("CallerCo", chi(9, 25))
    assert await reminders.run_round_reminders(now) == {"sent": 2, "failed": 0}
    posts = slack.posts()
    assert [p["thread_ts"] for p in posts] == ["77.1", "77.1"]
    assert all(p["channel"] == "C_DAILY" for p in posts)
    first = posts[0]["blocks"][0]["text"]["text"]
    assert "Starting in 20 min" in first and "SelfCo" in first
    assert "Starting in 25 min" in posts[1]["blocks"][0]["text"]["text"]
    assert posts[0]["text"] == "Starting in 20 min — SelfCo · Intro"
    assert posts[0]["unfurl_links"] is False


async def test_no_day_post_posts_top_level(slack):
    await add("Acme", chi(9, 20))
    assert await reminders.run_round_reminders(chi(9)) == {"sent": 1, "failed": 0}
    post = slack.posts()[0]
    assert "thread_ts" not in post and post["channel"] == "C_DAILY"


async def test_not_reminded_twice(slack):
    await add("Acme", chi(9, 40))
    assert (await reminders.run_round_reminders(chi(9, 15)))["sent"] == 1
    assert (await reminders.run_round_reminders(chi(9, 20)))["sent"] == 0
    assert len(slack.posts()) == 1
    kinds = [row.kind for row in await SlackAlertLog.find_all().to_list()]
    assert len(kinds) == 1 and kinds[0].startswith("round-reminder:")


async def test_rescheduled_round_reminded_again_at_new_time(slack):
    iv = await add("Acme", chi(10))
    assert (await reminders.run_round_reminders(chi(9, 35)))["sent"] == 1
    iv.stageHistory[0].scheduledAt = chi(11).astimezone(timezone.utc)
    await iv.save()
    assert (await reminders.run_round_reminders(chi(9, 50)))["sent"] == 0
    assert (await reminders.run_round_reminders(chi(10, 31)))["sent"] == 1
    assert len(slack.posts()) == 2


async def test_failed_post_releases_claim(slack):
    await add("Acme", chi(9, 20))
    slack.errors["chat.postMessage"] = ["ratelimited"]
    assert await reminders.run_round_reminders(chi(9)) == {"sent": 0, "failed": 1}
    assert await SlackAlertLog.find_all().to_list() == []
    assert await reminders.run_round_reminders(chi(9, 5)) == {"sent": 1, "failed": 0}


async def test_window_is_after_now_through_thirty_minutes(slack):
    now = chi(9)
    await add("Started", chi(9))  # starts exactly now: not "starting in"
    await add("Edge", chi(9, 30))  # exactly 30 min: in
    await add("TooFar", chi(9, 30, 1))
    await add("Past", chi(8, 50))
    assert await reminders.run_round_reminders(now) == {"sent": 1, "failed": 0}
    assert "Edge" in slack.posts()[0]["text"]


async def test_seconds_round_up_and_minimum_one_minute(slack):
    await add("Soon", chi(9, 0, 20))
    await reminders.run_round_reminders(chi(9))
    assert slack.posts()[0]["text"].startswith("Starting in 1 min")


async def test_not_reminded_for_home_assessment_or_ended_rounds(slack):
    await add("Home", chi(9, 20), stage="home_assessment")
    await add("Gone", chi(9, 20), status="canceled")
    assert await reminders.run_round_reminders(chi(9)) == {"sent": 0, "failed": 0}
    assert slack.calls == []


async def test_off_when_daily_channel_not_configured(slack, monkeypatch):
    monkeypatch.setattr(slack_bot, "_caller_daily_configured", lambda: None)
    await add("Acme", chi(9, 20))
    assert await reminders.run_round_reminders(chi(9)) == {"sent": 0, "failed": 0}
    assert slack.calls == []


async def test_lost_day_post_falls_back_to_top_level(slack):
    await day_post()
    await add("Acme", chi(9, 20))
    slack.errors["chat.postMessage"] = ["thread_not_found"]
    assert await reminders.run_round_reminders(chi(9)) == {"sent": 1, "failed": 0}
    first, second = slack.posts()
    assert first["thread_ts"] == "77.1" and "thread_ts" not in second


async def test_round_just_after_midnight_threads_under_its_own_days_post(slack):
    # Reminded at 23:40 for a 00:10 round: today's post is the wrong thread, tomorrow's is the right one.
    await day_post()
    tomorrow = DAY + timedelta(days=1)
    await InterviewDayPost(day=tomorrow.isoformat(), channel="C_DAILY", ts=["88.1"], interviewIds=[]).insert()
    await add("Night", chi(0, 10).replace(day=tomorrow.day))
    assert await reminders.run_round_reminders(chi(23, 40)) == {"sent": 1, "failed": 0}
    assert slack.posts()[0]["thread_ts"] == "88.1"


async def test_round_after_midnight_without_its_post_yet_is_posted_top_level(slack):
    await day_post()  # only today's post exists; the next one is made at 05:00
    await add("Night", chi(0, 10).replace(day=DAY.day + 1))
    assert await reminders.run_round_reminders(chi(23, 40)) == {"sent": 1, "failed": 0}
    assert "thread_ts" not in slack.posts()[0]


async def test_a_failing_release_does_not_skip_the_rest_of_the_tick(slack, monkeypatch):
    async def broken_release(key):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(slack_bot, "_reminder_release", broken_release)
    await add("First", chi(9, 20))
    await add("Second", chi(9, 25))
    slack.errors["chat.postMessage"] = ["ratelimited", None]
    assert await reminders.run_round_reminders(chi(9)) == {"sent": 1, "failed": 1}
    assert "Second" in slack.posts()[1]["text"]
