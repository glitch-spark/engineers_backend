"""Team reminder channel: the daily list and the 30-minute reminders (Slack faked, mongomock)."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app import slack_bot  # noqa: E402
from app.caller_teams import reminders  # noqa: E402
from app.caller_teams import slack as team_slack  # noqa: E402
from app.models.caller import Caller  # noqa: E402
from app.models.caller_team import CallerTeam, TeamDayPost, TeamRoundPost  # noqa: E402
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent  # noqa: E402
from app.models.slack_alert_log import SlackAlertLog  # noqa: E402

SIX = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)  # 06:00 CST
ACC = "64b000000000000000000002"


class FakeSlack:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()
        self.n = 0

    async def api(self, method, body):
        self.calls.append((method, body))
        if method in self.fail:
            raise RuntimeError("slack down")
        self.n += 1
        if method == "chat.getPermalink":
            return {"ok": True, "permalink": f"https://callers.slack.com/p{body['message_ts']}"}
        return {"ok": True, "ts": f"{self.n}.000"}

    def of(self, method):
        return [b for m, b in self.calls if m == method]


@pytest.fixture
async def env(monkeypatch):
    await init_beanie(
        database=AsyncMongoMockClient()["t"],
        document_models=[Interview, Caller, CallerTeam, TeamRoundPost, TeamDayPost, SlackAlertLog],
    )
    fake = FakeSlack()
    monkeypatch.setattr(team_slack, "api", fake.api)
    monkeypatch.setattr(team_slack, "callers_configured", lambda: True)
    jamil = await Caller(name="Jamil", nameKey="jamil", slackUserId="U_JAMIL").insert()
    team = await CallerTeam(
        name="Alpha", nameKey="alpha", managerCallerId=str(jamil.id), channelId="CTEAM", reminderChannelId="CREM",
    ).insert()
    omar = await Caller(name="Omar", nameKey="omar", slackUserId="U_OMAR", teamId=str(team.id)).insert()
    solo = await Caller(name="Solo", nameKey="solo", slackUserId="U_SOLO").insert()
    return {"slack": fake, "team": team, "omar": omar, "solo": solo}


async def make_iv(caller, at, *, rid="r1", status="scheduled", company="Acme"):
    c = InterviewCaller(enabled=True, callerName=caller.name, callerId=str(caller.id), callerKind="external")
    rnd = InterviewStageEvent(id=rid, stage="tech_round_1", at=SIX, scheduledAt=at, status=status, caller=c)
    return await Interview(
        accountId=ACC, createdBy=ACC, scheduledAt=at or SIX, companyName=company, stageHistory=[rnd.model_dump()],
    ).insert()


def text_of(body):
    return body.get("text", "") + str(body.get("blocks", ""))


async def test_day_post_waits_until_daily_time(env):
    await make_iv(env["omar"], SIX + timedelta(hours=3))
    await reminders.run_team_day_posts(SIX - timedelta(minutes=1))
    assert env["slack"].calls == []
    await reminders.run_team_day_posts(SIX)
    posts = env["slack"].of("chat.postMessage")
    assert len(posts) == 1 and posts[0]["channel"] == "CREM"
    assert "Acme" in text_of(posts[0]) and "<@U_OMAR>" in text_of(posts[0]) and "<@U_JAMIL>" in text_of(posts[0])
    assert "elements" not in str(posts[0]["blocks"])


async def test_day_post_lists_only_team_rounds(env):
    await make_iv(env["omar"], SIX + timedelta(hours=3))
    await make_iv(env["solo"], SIX + timedelta(hours=4), company="SoloCo")
    await reminders.run_team_day_posts(SIX)
    body = text_of(env["slack"].of("chat.postMessage")[0])
    assert "Acme" in body and "SoloCo" not in body


async def test_day_post_local_day_edge(env):
    await make_iv(env["omar"], datetime(2026, 11, 23, 6, 30, tzinfo=timezone.utc), company="InDay")
    await make_iv(env["omar"], datetime(2026, 11, 23, 5, 30, tzinfo=timezone.utc), company="PrevDay")
    await make_iv(env["omar"], datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc), company="NextDay")
    await reminders.run_team_day_posts(SIX)
    body = text_of(env["slack"].of("chat.postMessage")[0])
    assert "InDay" in body and "PrevDay" not in body and "NextDay" not in body


async def test_day_post_edits_in_place(env):
    await make_iv(env["omar"], SIX + timedelta(hours=3))
    await reminders.run_team_day_posts(SIX)
    first = env["slack"].of("chat.postMessage")
    await make_iv(env["omar"], SIX + timedelta(hours=5), company="Later")
    await reminders.run_team_day_posts(SIX + timedelta(minutes=5))
    assert len(env["slack"].of("chat.postMessage")) == len(first) == 1
    upd = env["slack"].of("chat.update")
    assert len(upd) == 1 and upd[0]["ts"] == "1.000" and "Later" in text_of(upd[0])
    assert await TeamDayPost.count() == 1


async def test_refresh_team_day_edits_only_posted_day(env):
    await reminders.refresh_team_day(str(env["team"].id), SIX)
    assert env["slack"].calls == []  # nothing posted yet: refresh never posts
    await reminders.run_team_day_posts(SIX)
    await make_iv(env["omar"], SIX + timedelta(hours=5), company="Later")
    await reminders.refresh_team_day(str(env["team"].id), SIX)
    assert "Later" in text_of(env["slack"].of("chat.update")[0])


async def test_empty_day_says_no_interviews(env):
    await reminders.run_team_day_posts(SIX)
    assert "No team interviews today." in text_of(env["slack"].of("chat.postMessage")[0])


async def test_reminder_once_per_round_and_start(env):
    iv = await make_iv(env["omar"], SIX + timedelta(minutes=20))
    await TeamRoundPost(
        teamId=str(env["team"].id), interviewId=str(iv.id), roundId="r1", channel="CTEAM", ts="9.9",
        state="scheduled",
    ).insert()
    assert (await reminders.run_team_reminders(SIX))["sent"] == 1
    assert (await reminders.run_team_reminders(SIX + timedelta(minutes=1)))["sent"] == 0
    posts = env["slack"].of("chat.postMessage")
    assert len(posts) == 1 and posts[0]["channel"] == "CREM"
    body = text_of(posts[0])
    assert "<@U_OMAR>" in body and "<@U_JAMIL>" in body and "https://callers.slack.com/p9.9" in body
    assert env["slack"].of("chat.getPermalink")[0] == {"channel": "CTEAM", "message_ts": "9.9"}
    assert "elements" not in str(posts[0]["blocks"])


async def test_reminder_without_thread_omits_link(env):
    await make_iv(env["omar"], SIX + timedelta(minutes=20))
    await reminders.run_team_reminders(SIX)
    assert env["slack"].of("chat.getPermalink") == []
    assert "slack.com" not in text_of(env["slack"].of("chat.postMessage")[0])


async def test_rescheduled_round_gets_new_reminder(env):
    iv = await make_iv(env["omar"], SIX + timedelta(minutes=20))
    await reminders.run_team_reminders(SIX)
    iv.stageHistory[0].scheduledAt = SIX + timedelta(minutes=25)
    await iv.save()
    assert (await reminders.run_team_reminders(SIX))["sent"] == 1


async def test_failed_reminder_is_retried(env):
    await make_iv(env["omar"], SIX + timedelta(minutes=20))
    env["slack"].fail.add("chat.postMessage")
    assert await reminders.run_team_reminders(SIX) == {"sent": 0, "failed": 1}
    env["slack"].fail.clear()
    assert (await reminders.run_team_reminders(SIX))["sent"] == 1


async def test_tbd_round_no_reminder_not_listed(env):
    await make_iv(env["omar"], None)
    await reminders.run_team_day_posts(SIX)
    assert "Acme" not in text_of(env["slack"].of("chat.postMessage")[0])
    assert (await reminders.run_team_reminders(SIX))["sent"] == 0


async def test_past_or_far_round_no_reminder(env):
    await make_iv(env["omar"], SIX - timedelta(minutes=5), rid="a")
    await make_iv(env["omar"], SIX + timedelta(minutes=45), rid="b")
    assert (await reminders.run_team_reminders(SIX))["sent"] == 0


async def test_archived_team_skipped(env):
    await make_iv(env["omar"], SIX + timedelta(minutes=20))
    env["team"].archivedAt = SIX
    await env["team"].save()
    assert await reminders.run_team_day_posts(SIX) == {}
    assert (await reminders.run_team_reminders(SIX))["sent"] == 0
    assert env["slack"].calls == []


async def test_tick_isolates_team_failures(env):
    from app.slack_bot import run_alert_tick

    main_day = AsyncMock(return_value="posted")
    main_rem = AsyncMock(return_value={"sent": 2, "failed": 0})
    with (
        patch("app.interview_day.post.run_day_post", main_day),
        patch("app.interview_day.reminders.run_round_reminders", main_rem),
        patch("app.interview_day.post.refresh_today", AsyncMock()),
        patch("app.coordination.chase.run_coordination_chase", AsyncMock(return_value={})),
        patch("app.caller_teams.sync.sweep_team_posts", AsyncMock(return_value={})),
        patch("app.standup.nudge.run_standup_nudge", AsyncMock(return_value="off")),
        patch("app.caller_teams.reminders.run_team_day_posts", AsyncMock(side_effect=RuntimeError("boom"))),
        patch("app.caller_teams.reminders.run_team_reminders", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        out = await run_alert_tick()
    assert out["caller_daily"] == "posted" and out["caller_reminders"] == 2
    assert out["team_daily"] == "failed" and out["team_reminders"] == 0
