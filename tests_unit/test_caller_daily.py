"""The 05:00 America/Chicago list of confirmed caller interviews (Slack faked; loader against mongomock)."""

import asyncio
import os
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402

from app import slack_bot  # noqa: E402
from app.models.interview import InterviewCaller, InterviewStageEvent  # noqa: E402
from app.slack_bot import (  # noqa: E402
    caller_daily_bounds,
    caller_daily_due,
    caller_daily_text,
    run_caller_daily,
    slack_time,
)

CHI = ZoneInfo("America/Chicago")
FIVE = time(5, 0)
DAY = date(2026, 10, 7)


def chi(d, hh, mm=0):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=CHI)


# --- due / bounds ---------------------------------------------------------------------------------


def test_due_only_after_five_chicago():
    assert caller_daily_due(chi(DAY, 4, 59), at=FIVE, tz=CHI) is None
    assert caller_daily_due(chi(DAY, 5, 0), at=FIVE, tz=CHI) == DAY
    assert caller_daily_due(chi(DAY, 23, 59), at=FIVE, tz=CHI) == DAY  # catch-up the rest of the day
    assert caller_daily_due(chi(DAY, 5, 0).astimezone(timezone.utc), at=FIVE, tz=CHI) == DAY  # UTC input


def test_bounds_are_local_midnights():
    start, end = caller_daily_bounds(DAY, CHI)
    assert (start, end) == (datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc), datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc))


def test_bounds_on_dst_day_are_25_hours():
    start, end = caller_daily_bounds(date(2026, 11, 1), CHI)
    assert start == datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)


# --- text -----------------------------------------------------------------------------------------


def row(hh, company, **kw):
    base = dict(start=chi(DAY, hh), company=company, stage="Intro", profile="Ian Meron", callerName="Sam",
                method="Video call", mentions="<@U0OWNER001> <@U0MANAGER1>", link="https://slack.example/p1")
    base.update(kw)
    return base


def test_text_lists_rows_with_mentions_and_link():
    text = caller_daily_text(DAY, [row(14, "Beta"), row(9, "Acme", link=None)])
    lines = text.splitlines()
    assert lines[0] == ":telephone_receiver: *Caller interviews today — Wed Oct 7*"
    assert lines[1] == (f"{slack_time(chi(DAY, 9), with_day=False)} · Acme · Intro · Ian Meron · caller Sam · Video call"
                        " — <@U0OWNER001> <@U0MANAGER1>")
    assert lines[2].endswith(" — <@U0OWNER001> <@U0MANAGER1> · <https://slack.example/p1|thread>")
    assert "Beta" in lines[2]


def test_text_empty_day():
    assert caller_daily_text(DAY, []) == ":telephone_receiver: No confirmed caller interviews today (Wed Oct 7)."


# --- run ------------------------------------------------------------------------------------------


class Fake:
    def __init__(self, rows=(), post_ok=True):
        self.claimed, self.released, self.posts, self.rows, self.post_ok = set(), [], [], list(rows), post_ok

    async def load(self, start, end):
        self.window = (start, end)
        return self.rows

    async def claim(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    async def release(self, key):
        self.released.append(key)
        self.claimed.discard(key)

    async def post(self, channel, text):
        self.posts.append((channel, text))
        return self.post_ok

    async def people(self, iv, entry):
        return {"owner": SimpleNamespace(name="Pedro", email="p@x.io", slackUserId="U0OWNER001"),
                "coworkers": [SimpleNamespace(name="Pedro", email="p@x.io", slackUserId="U0OWNER001")],
                "managers": [SimpleNamespace(name="Maria", email="m@x.io", slackUserId=None)],
                "profile": "Ian Meron"}

    async def permalink(self, iv, entry):
        return "https://slack.example/p1"

    def run(self, now, configured=("C_DAILY", FIVE, CHI)):
        return asyncio.run(run_caller_daily(
            now, load=self.load, claim=self.claim, release=self.release, post=self.post,
            people=self.people, permalink=self.permalink, configured=lambda: configured,
        ))


def caller_round(hh=9, **kw):
    at = chi(DAY, hh).astimezone(timezone.utc)
    return InterviewStageEvent(stage="intro", at=at, scheduledAt=at, status="scheduled", confirmed=True,
                               caller=InterviewCaller(enabled=True, callerName="Sam", startsAt=at, method="video"), **kw)


IV = SimpleNamespace(id="iv1", companyName="Acme", createdBy="u1", accountId=None)


def test_posts_once_per_day():
    fake = Fake(rows=[(IV, caller_round())])
    assert fake.run(chi(DAY, 5, 2)) == "posted"
    assert fake.run(chi(DAY, 5, 7)) == "already_sent"
    assert len(fake.posts) == 1
    channel, text = fake.posts[0]
    assert channel == "C_DAILY" and "Acme" in text
    assert text.splitlines()[1].endswith(" — <@U0OWNER001> Maria · <https://slack.example/p1|thread>")  # de-duplicated
    assert fake.window == caller_daily_bounds(DAY, CHI)
    assert fake.claimed == {"caller-daily:2026-10-07"}


def test_not_due_before_five():
    fake = Fake()
    assert fake.run(chi(DAY, 4, 55)) == "not_due" and fake.posts == []


def test_catch_up_later_that_day_posts():
    fake = Fake()
    assert fake.run(chi(DAY, 11, 40)) == "posted"
    assert fake.posts[0][1] == ":telephone_receiver: No confirmed caller interviews today (Wed Oct 7)."


def test_failed_post_releases_claim():
    fake = Fake(post_ok=False)
    assert fake.run(chi(DAY, 5, 2)) == "failed"
    assert fake.released == ["caller-daily:2026-10-07"] and fake.claimed == set()


def test_off_without_channel():
    fake = Fake()
    assert fake.run(chi(DAY, 6), configured=None) == "off" and fake.posts == []


# --- loader (mongomock) -------------------------------------------------------------------------


@pytest.fixture
async def db():
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    from app.models.interview import Interview

    database = AsyncMongoMockClient()["daily-unit"]
    await init_beanie(database=database, document_models=[Interview])
    return database


async def test_only_confirmed_active_rounds_are_loaded(db):
    from beanie import PydanticObjectId

    from app.models.interview import Interview

    def add(*rounds, company):
        return Interview(accountId=PydanticObjectId(), createdBy=PydanticObjectId(), scheduledAt=rounds[-1].scheduledAt,
                         companyName=company, stageHistory=[r.model_dump() for r in rounds]).insert()

    await add(caller_round(9), company="Yes")
    await add(caller_round(10, ).model_copy(update={"confirmed": False}), company="Unconfirmed")
    await add(caller_round(11).model_copy(update={"status": "canceled"}), company="Canceled")
    await add(caller_round(12).model_copy(update={"caller": InterviewCaller(enabled=False)}), company="NoCaller")
    other = caller_round(9)
    tomorrow = (chi(DAY, 9) + timedelta(days=1)).astimezone(timezone.utc)
    await add(other.model_copy(update={"scheduledAt": tomorrow, "at": tomorrow}), company="Tomorrow")
    # two rounds in one interview: only today's confirmed one is listed
    past = (chi(DAY, 9) - timedelta(days=3)).astimezone(timezone.utc)
    await add(caller_round(9).model_copy(update={"scheduledAt": past, "at": past}), caller_round(16), company="Both")

    rows = await slack_bot._caller_daily_load(*caller_daily_bounds(DAY, CHI))
    assert sorted((iv.companyName, e.scheduledAt.hour) for iv, e in rows) == [("Both", 21), ("Yes", 14)]


# --- cron order -------------------------------------------------------------------------------


def test_alert_tick_runs_daily_after_reminders():
    order = []
    reminders = AsyncMock(side_effect=lambda now: order.append("reminders") or {"sent": 0, "failed": 0})
    daily = AsyncMock(side_effect=lambda now: order.append("daily") or "posted")
    with (
        patch.object(slack_bot, "run_caller_reminders", reminders),
        patch.object(slack_bot, "run_caller_daily", daily),
        patch.object(slack_bot, "slack_bot_configured", lambda: False),
    ):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert order == ["reminders", "daily"] and out["caller_daily"] == "posted"


def test_alert_tick_survives_daily_crash():
    with (
        patch.object(slack_bot, "run_caller_reminders", AsyncMock(return_value={"sent": 0, "failed": 0})),
        patch.object(slack_bot, "run_caller_daily", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(slack_bot, "slack_bot_configured", lambda: False),
    ):
        assert asyncio.run(slack_bot.run_alert_tick())["caller_daily"] == "failed"
