"""The Friday result roundup is no longer posted (the 1-week reminder in each round's daily-channel thread replaces
it), but roundups already in Slack keep being edited in place as their rounds are answered (Slack faked: `slack_bot._slack_api` is a recorder, `get_settings` is patched, no .env is read)."""

import os
from datetime import date, datetime, time, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app import slack_bot  # noqa: E402
from app.coordination import actions, chase  # noqa: E402
from app.models import Account, Interview, ResultRoundupPost, SlackAlertLog, UserExtra  # noqa: E402
from app.models.caller import Caller  # noqa: E402
from app.models.interview import InterviewCaller, InterviewStageEvent, SlackThread  # noqa: E402
from app.models.interview_day_post import InterviewDayPost  # noqa: E402
from app.models.interview_question import InterviewQuestion  # noqa: E402
from app.routers import interviews  # noqa: E402

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

UTC = timezone.utc
CUTOFF = "2026-11-02T00:00"
THANKS = "✅ All results are in — thanks."
RESPONSE_URL = "https://hooks.slack.com/actions/T1/1/abc"


def chi(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=chase.CHASE_TZ)


FRIDAY = chi(2026, 11, 20, 18, 0)


def settings(**over):
    values = dict(SLACK_BOT_TOKEN="x", SLACK_CALLER_CHANNEL_ID="C1", SLACK_CALLER_ASKS_FROM=CUTOFF)
    values.update(over)
    return SimpleNamespace(**values)


class Slack:
    def __init__(self):
        self.calls, self._n = [], count(1)

    async def __call__(self, method, body):
        self.calls.append((method, body))
        return {"ts": f"9.{next(self._n)}"}

    def of(self, method):
        return [b for m, b in self.calls if m == method]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["coord-roundup-unit"]
    await init_beanie(database=database, document_models=[
        Interview, UserExtra, Account, SlackAlertLog, ResultRoundupPost, InterviewQuestion, InterviewDayPost, Caller,
    ])
    return database


@pytest.fixture
def slack(monkeypatch, db):
    fake = Slack()
    monkeypatch.setattr(slack_bot, "_slack_api", fake)
    monkeypatch.setattr(chase, "get_settings", lambda: settings())
    monkeypatch.setattr(slack_bot, "_caller_daily_configured", lambda: ("C_DAILY", time(5, 0), chase.CHASE_TZ))
    return fake


async def make_user(name, slack_id, role="staff") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, slackUserId=slack_id).insert()


@pytest.fixture
async def justin(db):
    return await make_user("Justin", "U0JUSTIN01")


@pytest.fixture
async def ana(db):
    return await make_user("Ana", "U0ANA00001")


def day(n, hh=15):
    return datetime(2026, 11, n, hh, 0, tzinfo=UTC)


async def add(owner, *, start=day(10), status="completed", caller=True, company="Acme", stage="intro") -> Interview:
    entry = InterviewStageEvent(
        stage=stage, at=start, scheduledAt=start, endsAt=start + timedelta(hours=1), status=status,
        caller=InterviewCaller(enabled=caller, callerName="Sam", startsAt=start, method="video") if caller else None,
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=start, companyName=company,
        stageHistory=[entry.model_dump()], stage=stage, status=status,
        slackThread=SlackThread(channel="C1", ts="100.1"),
    ).insert()


def ref(iv) -> str:
    return f"{iv.id}:{iv.stageHistory[-1].id}"


async def set_status(iv, status):
    raw = iv.model_dump()["stageHistory"]
    raw[-1]["status"] = status
    await Interview.find_one(Interview.id == iv.id).update({"$set": {"stageHistory": raw}})


def sections(post):
    return [b["text"]["text"] for b in post["blocks"] if b["type"] == "section"]


def actions_blocks(post):
    return [b for b in post["blocks"] if b["type"] == "actions"]


async def post_roundup(when=None, friday="2026-11-20"):
    """An existing roundup as the removed Friday post left it: posted in #caller ("C1") and stored."""
    when = when or FRIDAY
    cutoff = chase.asks_from()
    rounds = await chase.ended_rounds("completed", ended_after=cutoff, ended_by=when)
    text, blocks, refs = chase._roundup_message(rounds, await chase._profiles(rounds), date.fromisoformat(friday))
    sent = await slack_bot._slack_api("chat.postMessage", {"channel": "C1", "text": text, "blocks": blocks})
    await ResultRoundupPost(friday=friday, channel="C1", ts=sent["ts"], refs=refs,
                            rendered=chase._rendered(blocks)).insert()


async def seed_open(justin, ana):
    """Justin 2 and Ana 1 open results, plus two rounds that must not be listed."""
    ivs = [await add(justin, start=day(12), company="Beta"), await add(justin, start=day(10), company="Alpha"),
           await add(ana, start=day(11), company="Gamma")]
    await add(justin, status="passed")
    await add(justin, start=datetime(2026, 11, 1, 19, 0, tzinfo=UTC))  # ends 20:00Z, before the cutoff
    return ivs



# --- the post ----------------------------------------------------------------------------------------


# --- edited in place ---------------------------------------------------------------------------------


async def test_answered_round_drops_off(slack, justin, ana):
    ivs = await seed_open(justin, ana)
    await post_roundup()
    await set_status(ivs[0], "passed")

    await chase.refresh_latest_roundup()
    [upd] = slack.of("chat.update")
    assert upd["channel"] == "C1" and upd["ts"] == "9.1"
    assert len(actions_blocks(upd)) == 2
    assert len(slack.of("chat.postMessage")) == 1

    await chase.refresh_latest_roundup()
    assert len(slack.of("chat.update")) == 1  # unchanged, no call
    stored = await ResultRoundupPost.find_one({"friday": "2026-11-20"})
    assert len(stored.refs) == 3  # the refs listed when posted


async def test_all_answered_says_thanks(slack, justin, ana):
    ivs = await seed_open(justin, ana)
    await post_roundup()
    for iv in ivs:
        await set_status(iv, "rejected")
    await chase.refresh_latest_roundup()
    [upd] = slack.of("chat.update")
    assert upd["text"] == THANKS
    assert [b["type"] for b in upd["blocks"]] == ["section"]
    assert sections(upd) == [THANKS]


async def test_a_round_that_lost_its_caller_stays_until_answered(slack, justin, ana):
    ivs = await seed_open(justin, ana)
    await post_roundup()
    raw = ivs[2].model_dump()["stageHistory"]
    raw[-1]["caller"]["enabled"] = False
    await Interview.find_one(Interview.id == ivs[2].id).update({"$set": {"stageHistory": raw}})
    await set_status(ivs[0], "passed")
    await chase.refresh_latest_roundup()
    assert len(actions_blocks(slack.of("chat.update")[0])) == 2  # only the answered one left


async def test_a_deleted_interview_drops_off(slack, justin, ana):
    ivs = await seed_open(justin, ana)
    await post_roundup()
    await ivs[0].delete()
    await chase.refresh_latest_roundup()
    assert len(actions_blocks(slack.of("chat.update")[0])) == 2


async def test_roundup_block_limit(slack, justin):
    for i in range(30):
        await add(justin, start=day(10 + i % 9, 10 + i % 5), company=f"Co{i:02d}")
    await post_roundup()
    [post] = slack.of("chat.postMessage")
    blocks = post["blocks"]
    assert len(blocks) <= 50
    assert blocks[-1]["type"] == "context"
    text = blocks[-1]["elements"][0]["text"]
    assert text.startswith("…and ")
    assert " more without a result — <" in text and text.endswith("/statistics|Statistics>")
    shown = len(actions_blocks(post))
    k = int(text.split()[1])
    assert shown + k == 30 and k > 0
    stored = await ResultRoundupPost.find_one({})
    assert len(stored.refs) == 30  # hidden rounds surface as the shown ones are answered


async def test_hidden_rounds_surface_when_shown_ones_are_answered(slack, justin):
    ivs = [await add(justin, start=day(10 + i % 9, 10 + i % 5), company=f"Co{i:02d}") for i in range(30)]
    await post_roundup()
    first = slack.of("chat.postMessage")[0]
    shown = [b["elements"][0]["value"] for b in actions_blocks(first)]
    for iv in ivs:
        if ref(iv) in shown[:3]:
            await set_status(iv, "passed")
    await chase.refresh_latest_roundup()
    [upd] = slack.of("chat.update")
    assert len(actions_blocks(upd)) == len(shown)  # still full: three hidden rounds moved up
    assert len(upd["blocks"]) <= 50


# --- off, and the click ------------------------------------------------------------------------------


async def test_refresh_is_a_no_op_when_off(slack, justin, monkeypatch):
    await add(justin)
    await post_roundup()
    await set_status(await Interview.find_one({}), "passed")
    monkeypatch.setattr(chase, "get_settings", lambda: settings(SLACK_CALLER_ASKS_FROM=None))
    await chase.refresh_latest_roundup()
    assert slack.of("chat.update") == []


async def test_click_on_the_roundup_refreshes_it(slack, justin, ana, monkeypatch):
    monkeypatch.setattr(interviews, "sync_caller_thread", AsyncMock(return_value=False))
    monkeypatch.setattr(interviews, "refresh_today", AsyncMock(return_value=False))
    replies = []

    async def respond(url, payload):
        replies.append((url, payload))

    ivs = await seed_open(justin, ana)
    await post_roundup()
    post = await ResultRoundupPost.find_one({})

    click = actions.Click("coord_result_passed", ref(ivs[0]), "U0JUSTIN01", RESPONSE_URL, "T.1", "C1", post.ts)
    with patch.object(actions, "_post_ephemeral", respond):
        await actions.process_coordination_click(click)

    stored = (await Interview.get(ivs[0].id)).stageHistory[-1].status
    assert getattr(stored, "value", stored) == "passed"
    [upd] = slack.of("chat.update")
    assert upd["ts"] == post.ts and len(actions_blocks(upd)) == 2
    assert ref(ivs[0]) not in [b["elements"][0]["value"] for b in actions_blocks(upd)]
    assert replies == []  # no replace, no ephemeral
    assert len(slack.of("chat.postMessage")) == 1


async def test_a_click_elsewhere_does_not_touch_the_roundup(slack, justin, ana, monkeypatch):
    monkeypatch.setattr(interviews, "sync_caller_thread", AsyncMock(return_value=False))
    monkeypatch.setattr(interviews, "refresh_today", AsyncMock(return_value=False))
    replies = []

    async def respond(url, payload):
        replies.append((url, payload))

    ivs = await seed_open(justin, ana)
    await post_roundup()
    click = actions.Click("coord_result_passed", ref(ivs[0]), "U0JUSTIN01", RESPONSE_URL, "T.1", "C1", "555.5")
    with patch.object(actions, "_post_ephemeral", respond):
        await actions.process_coordination_click(click)
    assert slack.of("chat.update") == []
    assert len(replies) == 1 and replies[0][1]["replace_original"] is True


async def test_a_deleted_roundup_stops_being_edited(slack, justin, monkeypatch):
    iv = await add(justin)
    await post_roundup()
    await set_status(iv, "passed")
    calls = []

    async def gone(method, body):
        calls.append(method)
        raise RuntimeError("message_not_found")

    monkeypatch.setattr(slack_bot, "_slack_api", gone)
    await chase.refresh_latest_roundup()  # the one failed update
    assert calls == ["chat.update"]
    assert await ResultRoundupPost.find_all().to_list() == []
    await chase.refresh_latest_roundup()  # next tick: nothing to edit
    assert calls == ["chat.update"]
    assert len(slack.of("chat.postMessage")) == 1  # nothing re-posted


async def test_other_update_errors_keep_the_roundup(slack, justin, monkeypatch):
    iv = await add(justin)
    await post_roundup()
    await set_status(iv, "passed")
    monkeypatch.setattr(slack_bot, "_slack_api", AsyncMock(side_effect=RuntimeError("ratelimited")))
    with pytest.raises(RuntimeError):
        await chase.refresh_latest_roundup()
    assert len(await ResultRoundupPost.find_all().to_list()) == 1
