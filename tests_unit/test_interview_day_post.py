"""The day post: posted once at 05:00 America/Chicago, then edited in place as interviews change (Slack faked)."""

import os
from datetime import date, datetime, time, timedelta, timezone
from itertools import count
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import slack_bot
from app.interview_day import post
from app.models import Account, Interview, SlackAlertLog, UserExtra
from app.models.interview import InterviewCaller, InterviewStageEvent, SlackThread
from app.models.interview_day_post import InterviewDayPost

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
DAY = date(2026, 10, 7)
CONFIG = ("C_DAILY", time(5, 0), CHI)


def chi(hh, mm=0, day=DAY):
    return datetime.combine(day, time(hh, mm), tzinfo=CHI)


class Slack:
    """Records every Slack call. chat.postMessage hands out ts 1, 2, ...; `errors[method]` is a queue with one
    entry per call: an error string raises it, None lets that call through."""

    def __init__(self):
        self.calls, self.errors, self.replies, self._n = [], {}, {}, count(1)

    async def __call__(self, method, body):
        self.calls.append((method, body))
        queue = self.errors.get(method)
        if queue and (err := queue.pop(0)):
            raise RuntimeError(err)
        if method in self.replies:
            return self.replies[method]
        return {"ts": str(next(self._n))} if method == "chat.postMessage" else {}

    def of(self, method):
        return [b for m, b in self.calls if m == method]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["day-post-unit"]
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


async def add(company, hh, *, day=DAY, caller=True, status="scheduled"):
    at = chi(hh, day=day).astimezone(timezone.utc)
    entry = InterviewStageEvent(
        stage="intro", at=at, scheduledAt=at, status=status,
        caller=InterviewCaller(enabled=caller, callerName="Sam", startsAt=at, method="video") if caller else None,
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=PydanticObjectId(), scheduledAt=at, companyName=company,
        stageHistory=[entry.model_dump()], stage="intro", status=status,
    ).insert()


def text_of(blocks):
    return " ".join(b.get("text", {}).get("text", "") for b in blocks if b["type"] in ("section", "header"))


async def stored_post():
    return await InterviewDayPost.find_one(InterviewDayPost.day == "2026-10-07")


async def test_posts_once_per_day_and_stores_ts(slack):
    await add("Acme", 9)
    assert await post.run_day_post(chi(5, 2)) == "posted"
    assert await post.run_day_post(chi(5, 7)) == "already_sent"
    posted = slack.of("chat.postMessage")
    assert len(posted) == 1 and posted[0]["channel"] == "C_DAILY"
    assert "Acme" in text_of(posted[0]["blocks"])
    assert posted[0]["text"] == "Interviews today — Wed Oct 7 · 1 round"
    stored = await stored_post()
    assert stored.ts == ["1"] and stored.channel == "C_DAILY"
    assert await SlackAlertLog.find(SlackAlertLog.kind == "interview-day:2026-10-07").count() == 1


async def test_empty_day_still_posts_and_not_due_before_five(slack):
    assert await post.run_day_post(chi(4, 55)) == "not_due" and slack.calls == []
    assert await post.run_day_post(chi(11, 40)) == "posted"
    assert text_of(slack.of("chat.postMessage")[0]["blocks"]) == "No interviews today."


async def test_off_without_channel(slack, monkeypatch):
    monkeypatch.setattr(slack_bot, "_caller_daily_configured", lambda: None)
    assert await post.run_day_post(chi(6)) == "off" and slack.calls == []


async def test_failed_post_releases_claim_and_removes_partial(slack):
    for i in range(30):
        await add(f"Co{i:02d}", 6 + i // 4)
    slack.errors["chat.postMessage"] = [None, "ratelimited"]  # the first message goes out, the second fails
    assert await post.run_day_post(chi(5, 2)) == "failed"
    assert [b["ts"] for b in slack.of("chat.delete")] == ["1"]
    assert await InterviewDayPost.find_all().to_list() == []
    assert await SlackAlertLog.find_all().to_list() == []
    # the next tick starts clean
    assert await post.run_day_post(chi(5, 7)) == "posted"


async def test_refresh_drops_round_moved_to_tomorrow(slack):
    await add("Keep", 9)
    move = await add("Move", 10)
    await post.run_day_post(chi(5, 2))
    move.stageHistory[0].scheduledAt = chi(10, day=DAY + timedelta(days=1)).astimezone(timezone.utc)
    await move.save()
    slack.calls.clear()

    assert await post.refresh_today(chi(8)) is True
    assert [m for m, _ in slack.calls] == ["chat.update"]
    update = slack.of("chat.update")[0]
    assert update["channel"] == "C_DAILY" and update["ts"] == "1"
    assert "Keep" in text_of(update["blocks"]) and "Move" not in text_of(update["blocks"])
    assert update["text"] == "Interviews today — Wed Oct 7 · 1 round"


async def test_refresh_deletes_surplus_message(slack):
    ivs = [await add(f"Co{i:02d}", 6 + i // 4) for i in range(30)]
    await post.run_day_post(chi(5, 2))
    assert (await stored_post()).ts == ["1", "2"]
    for iv in ivs[:25]:
        iv.stageHistory[0].status = "canceled"
        await iv.save()
    slack.calls.clear()

    assert await post.refresh_today(chi(8)) is True
    assert [b["ts"] for b in slack.of("chat.delete")] == ["2"]
    assert len(slack.of("chat.update")) == 1
    assert (await stored_post()).ts == ["1"]


async def test_refresh_posts_extra_message_when_day_grows(slack):
    for i in range(3):
        await add(f"Co{i}", 9)
    await post.run_day_post(chi(5, 2))
    for i in range(30):
        await add(f"More{i:02d}", 10 + i // 10)
    slack.calls.clear()
    assert await post.refresh_today(chi(8)) is True
    assert len(slack.of("chat.update")) == 1 and len(slack.of("chat.postMessage")) == 1
    assert (await stored_post()).ts == ["1", "2"]


async def test_refresh_reposts_a_message_slack_lost(slack):
    await add("Acme", 9)
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    slack.errors["chat.update"] = ["message_not_found"]
    assert await post.refresh_today(chi(8)) is True
    assert [m for m, _ in slack.calls] == ["chat.update", "chat.postMessage"]
    assert (await stored_post()).ts == ["2"]


async def test_refresh_keeps_progress_when_slack_fails_midway(slack):
    for i in range(30):
        await add(f"Co{i:02d}", 6 + i // 4)
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    slack.errors["chat.update"] = [None, "ratelimited"]
    with pytest.raises(RuntimeError):
        await post.refresh_today(chi(8))
    assert (await stored_post()).ts == ["1", "2"]


async def test_refresh_without_post_is_noop(slack):
    await add("Acme", 9)
    assert await post.refresh_today(chi(8)) is False
    assert slack.calls == []


async def test_refresh_when_slack_is_off_is_noop(slack, monkeypatch):
    await add("Acme", 9)
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    monkeypatch.setattr(slack_bot, "_caller_daily_configured", lambda: None)
    assert await post.refresh_today(chi(8)) is False and slack.calls == []


async def test_refresh_ignores_yesterdays_post(slack):
    await add("Acme", 9)
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    assert await post.refresh_today(chi(8, day=DAY + timedelta(days=1))) is False
    assert slack.calls == []


async def test_today_is_the_local_date_not_utc(slack):
    await add("Late", 22)
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    # 02:00 UTC on Oct 8 is still Oct 7 in Chicago
    assert await post.refresh_today(datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)) is True


async def test_today_parent_ts_is_the_first_message(slack):
    assert await post.today_parent_ts(chi(8)) is None
    await post.run_day_post(chi(5, 2))
    assert await post.today_parent_ts(chi(8)) == "1"
    assert await post.today_parent_ts(chi(8, day=DAY + timedelta(days=1))) is None


async def test_one_permalink_per_interview_for_all_its_rounds(slack):
    iv = await add("Acme", 9)
    iv.slackThread = SlackThread(channel="C_CALLER", ts="100.1")
    iv.stageHistory.append(iv.stageHistory[0].model_copy(update={"id": "r2", "stage": "tech_round_1"}))
    await iv.save()
    other = await add("Beta", 11)
    other.slackThread = SlackThread(channel="C_CALLER", ts="200.1")
    await other.save()
    slack.replies["chat.getPermalink"] = {"permalink": "https://slack.example/p1"}
    await post.run_day_post(chi(5, 2))
    assert sorted(b["message_ts"] for b in slack.of("chat.getPermalink")) == ["100.1", "200.1"]
    slack.calls.clear()
    await post.refresh_today(chi(8))
    assert len(slack.of("chat.getPermalink")) == 2


async def test_post_remembers_which_interviews_it_lists(slack):
    a, b = await add("A", 9), await add("B", 10)
    await add("Tomorrow", 9, day=DAY + timedelta(days=1))
    await post.run_day_post(chi(5, 2))
    assert (await stored_post()).interviewIds == sorted([str(a.id), str(b.id)])
    b.stageHistory[0].status = "canceled"
    await b.save()
    await post.refresh_today(chi(8))
    assert (await stored_post()).interviewIds == [str(a.id)]


async def test_refresh_for_an_unrelated_interview_does_nothing(slack):
    await add("A", 9)
    other = await add("Tomorrow", 9, day=DAY + timedelta(days=1))
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    assert await post.refresh_today(chi(8), interview_id=str(other.id)) is False
    assert await post.refresh_today(chi(8), interview_id=str(PydanticObjectId()), deleted=True) is False
    assert slack.calls == []


async def test_refresh_for_a_listed_or_newly_today_interview_edits_the_post(slack):
    listed = await add("A", 9)
    mover = await add("Mover", 9, day=DAY + timedelta(days=1))
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    assert await post.refresh_today(chi(8), interview_id=str(listed.id), deleted=True) is True  # was in the post
    mover.stageHistory[0].scheduledAt = chi(14).astimezone(timezone.utc)
    await mover.save()
    assert await post.refresh_today(chi(8), interview_id=str(mover.id)) is True  # now has a round today
    assert len(slack.of("chat.update")) == 2
    assert (await stored_post()).interviewIds == sorted([str(listed.id), str(mover.id)])


async def test_failed_refresh_keeps_an_interview_listed_that_may_still_show(slack):
    await add("Keep", 9)
    drop = await add("Drop", 10)
    await post.run_day_post(chi(5, 2))
    drop.stageHistory[0].status = "canceled"
    await drop.save()
    slack.errors["chat.update"] = ["ratelimited"]
    with pytest.raises(RuntimeError):
        await post.refresh_today(chi(8))
    assert str(drop.id) in (await stored_post()).interviewIds


async def test_caller_rounds_link_to_their_caller_thread(slack):
    iv = await add("Acme", 9)
    iv.slackThread = SlackThread(channel="C_CALLER", ts="100.1")
    await iv.save()
    await add("Mine", 10, caller=False)
    slack.replies["chat.getPermalink"] = {"permalink": "https://slack.example/p1"}
    await post.run_day_post(chi(5, 2))
    assert [b["channel"] for b in slack.of("chat.getPermalink")] == ["C_CALLER"]  # none for the self round
    assert text_of(slack.of("chat.postMessage")[0]["blocks"]).count("<https://slack.example/p1|thread>") == 1


# --- the interview write hook (router) ---------------------------------------------------------


@pytest.fixture
def api(db, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import interviews

    monkeypatch.setattr(interviews, "sync_caller_thread", AsyncMock(return_value=False))

    def make(user):
        app = FastAPI()
        app.include_router(interviews.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


@pytest.fixture
def stubbed_refresh(api, monkeypatch):
    from app.routers import interviews

    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(interviews, "refresh_today", refresh)
    return refresh


@pytest.fixture
async def admin(db):
    from app.models.interview_question import InterviewQuestion

    await init_beanie(
        database=db,
        document_models=[Interview, UserExtra, Account, InterviewQuestion, SlackAlertLog, InterviewDayPost],
    )
    return await UserExtra(name="Pedro", email="p@x.io", role="admin").insert()


async def owned(admin, company, hh, **kw):
    iv = await add(company, hh, **kw)
    iv.createdBy = admin.id
    await iv.save()
    return iv


def move_to(day, hh=15):
    return {"scheduledAt": chi(hh, day=day).astimezone(timezone.utc).isoformat()}


async def patch_round(client, iv, body):
    r = await client.patch(f"/interviews/{iv.id}/stages/{iv.stageHistory[0].id}", json=body)
    assert r.status_code == 200, r.text


async def test_interview_write_refreshes_today(api, stubbed_refresh, admin):
    from app.routers import interviews

    iv = await owned(admin, "Acme", 9)
    async with api(admin) as client:
        await patch_round(client, iv, move_to(DAY + timedelta(days=2)))
    await interviews.drain_day_post_refreshes()
    stubbed_refresh.assert_awaited_once_with(interview_id=str(iv.id), deleted=False)


async def test_the_write_does_not_wait_for_slack(api, stubbed_refresh, admin):
    import asyncio

    from app.routers import interviews

    release = asyncio.Event()

    async def slow(**kw):
        await release.wait()
        return True

    stubbed_refresh.side_effect = slow
    iv = await owned(admin, "Acme", 9)
    async with api(admin) as client:
        await patch_round(client, iv, move_to(DAY + timedelta(days=2)))  # answered while the refresh is stuck
    release.set()
    await interviews.drain_day_post_refreshes()
    stubbed_refresh.assert_awaited_once()


async def test_a_failing_refresh_never_fails_the_write(api, stubbed_refresh, admin):
    from app.routers import interviews

    stubbed_refresh.side_effect = RuntimeError("slack down")
    iv = await owned(admin, "Acme", 9)
    async with api(admin) as client:
        await patch_round(client, iv, move_to(DAY + timedelta(days=2)))
    await interviews.drain_day_post_refreshes()  # logs the error, raises nothing


async def test_deleting_a_round_refreshes_after_it_is_gone(api, stubbed_refresh, admin):
    from app.routers import interviews

    iv = await owned(admin, "Acme", 9)
    first = iv.stageHistory[0].id
    iv.stageHistory.append(iv.stageHistory[0].model_copy(update={"id": "r2", "stage": "tech_round_1"}))
    await iv.save()
    seen = []

    async def snapshot(**kw):
        doc = await Interview.get(iv.id)
        seen.append((kw, None if doc is None else [e.id for e in doc.stageHistory]))
        return True

    stubbed_refresh.side_effect = snapshot
    kw = {"interview_id": str(iv.id), "deleted": True}
    async with api(admin) as client:
        r = await client.delete(f"/interviews/{iv.id}/stages/r2")
        assert r.status_code == 200, r.text
        await interviews.drain_day_post_refreshes()
        assert seen == [(kw, [first])]  # the round was already gone
        r = await client.delete(f"/interviews/{iv.id}")
        assert r.status_code == 200, r.text
        await interviews.drain_day_post_refreshes()
    assert seen[1] == (kw, None)


# Through the real refresh, with Slack faked: only writes that touch today reach Slack.


@pytest.fixture
def real_refresh(slack, monkeypatch):
    monkeypatch.setattr(post, "_now", lambda now: now or chi(8))


async def test_write_to_an_interview_with_only_other_days_makes_no_slack_calls(api, real_refresh, slack, admin):
    from app.routers import interviews

    await owned(admin, "Today", 9)
    other = await owned(admin, "Later", 9, day=DAY + timedelta(days=1))
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    async with api(admin) as client:
        await patch_round(client, other, move_to(DAY + timedelta(days=3)))
    await interviews.drain_day_post_refreshes()
    assert slack.calls == []


async def test_moving_a_round_off_today_refreshes_the_post(api, real_refresh, slack, admin):
    from app.routers import interviews

    keep = await owned(admin, "Keep", 9)
    move = await owned(admin, "Move", 10)
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    async with api(admin) as client:
        await patch_round(client, move, move_to(DAY + timedelta(days=1)))
    await interviews.drain_day_post_refreshes()
    update = slack.of("chat.update")[0]
    assert "Keep" in text_of(update["blocks"]) and "Move" not in text_of(update["blocks"])
    assert (await stored_post()).interviewIds == [str(keep.id)]


async def test_moving_a_round_onto_today_refreshes_the_post(api, real_refresh, slack, admin):
    from app.routers import interviews

    await owned(admin, "Today", 9)
    later = await owned(admin, "Later", 9, day=DAY + timedelta(days=1))
    await post.run_day_post(chi(5, 2))
    slack.calls.clear()
    async with api(admin) as client:
        await patch_round(client, later, move_to(DAY, 16))
    await interviews.drain_day_post_refreshes()
    assert "Later" in text_of(slack.of("chat.update")[0]["blocks"])
