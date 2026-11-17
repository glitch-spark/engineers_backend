"""The Friday result roundup: one message listing the caller rounds with no result, edited in place as they are
answered (Slack faked: `slack_bot._slack_api` is a recorder, `get_settings` is patched, no .env is read)."""

import os
from datetime import date, datetime, timedelta, timezone
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


async def seed_open(justin, ana):
    """Justin 2 and Ana 1 open results, plus three rounds that must not be listed."""
    ivs = [await add(justin, start=day(12), company="Beta"), await add(justin, start=day(10), company="Alpha"),
           await add(ana, start=day(11), company="Gamma")]
    await add(justin, status="passed")
    await add(justin, caller=False)
    await add(justin, start=datetime(2026, 11, 1, 19, 0, tzinfo=UTC))  # ends 20:00Z, before the cutoff
    return ivs


# --- when it is due ----------------------------------------------------------------------------------


def test_roundup_due_only_friday_evening():
    assert chase.roundup_due(chi(2026, 11, 20, 17, 59)) is None
    assert chase.roundup_due(chi(2026, 11, 20, 18, 0)) == date(2026, 11, 20)
    assert chase.roundup_due(chi(2026, 11, 20, 23, 59)) == date(2026, 11, 20)
    assert chase.roundup_due(chi(2026, 11, 21, 9, 0)) is None
    assert chase.roundup_due(datetime(2026, 11, 21, 0, 30, tzinfo=UTC)) == date(2026, 11, 20)  # Fri 18:30 Chicago


# --- the post ----------------------------------------------------------------------------------------


async def test_roundup_lists_open_results_by_owner(slack, justin, ana):
    ivs = await seed_open(justin, ana)
    assert await chase.run_result_roundup(FRIDAY) == "posted"

    [post] = slack.of("chat.postMessage")
    assert post["channel"] == "C1" and "thread_ts" not in post
    texts = sections(post)
    assert texts[0] == "*📋 Results still open — Fri Nov 20*"
    owners = [t for t in texts if " — " in t and "round" in t and "·" not in t]
    assert owners == ["<@U0ANA00001> — 1 round", "<@U0JUSTIN01> — 2 rounds"]
    assert texts.index(owners[0]) < texts.index(owners[1])
    rows = actions_blocks(post)
    assert len(rows) == 3
    assert all([e["action_id"] for e in b["elements"]] == ["coord_result_passed", "coord_result_rejected"]
               for b in rows)
    assert [b["elements"][0]["style"] for b in rows] == ["primary"] * 3
    assert [b["elements"][1]["style"] for b in rows] == ["danger"] * 3
    # Ana's round first, then Justin's by start (Alpha on the 10th, Beta on the 12th)
    assert [b["elements"][0]["value"] for b in rows] == [ref(ivs[2]), ref(ivs[1]), ref(ivs[0])]
    assert "Gamma · Intro" in texts[2]
    assert post["text"] == "📋 Results still open — Fri Nov 20"

    stored = await ResultRoundupPost.find_all().to_list()
    assert len(stored) == 1
    assert stored[0].friday == "2026-11-20" and stored[0].channel == "C1" and stored[0].ts == "9.1"
    assert len(stored[0].refs) == 3 and stored[0].rendered
    assert [r.kind for r in await SlackAlertLog.find_all().to_list()] == ["result-roundup:2026-11-20"]


async def test_roundup_once_per_friday(slack, justin, ana):
    await seed_open(justin, ana)
    assert await chase.run_result_roundup(FRIDAY) == "posted"
    assert await chase.run_result_roundup(chi(2026, 11, 20, 18, 5)) == "skipped"
    assert len(slack.of("chat.postMessage")) == 1
    assert await chase.run_result_roundup(chi(2026, 11, 20, 17, 0)) == "skipped"  # not due


async def test_nothing_to_list_no_post(slack, justin):
    await add(justin, status="passed")
    assert await chase.run_result_roundup(FRIDAY) == "none"
    assert slack.calls == []
    assert await ResultRoundupPost.find_all().to_list() == []
    assert await chase.run_result_roundup(chi(2026, 11, 20, 18, 5)) == "skipped"  # the claim is kept


async def test_failed_post_releases_the_claim(slack, justin, ana, monkeypatch):
    await seed_open(justin, ana)
    monkeypatch.setattr(slack_bot, "_slack_api", AsyncMock(side_effect=RuntimeError("boom")))
    assert await chase.run_result_roundup(FRIDAY) == "failed"
    assert await SlackAlertLog.find_all().to_list() == []
    assert await ResultRoundupPost.find_all().to_list() == []
    monkeypatch.setattr(slack_bot, "_slack_api", slack)
    assert await chase.run_result_roundup(chi(2026, 11, 20, 18, 5)) == "posted"


# --- edited in place ---------------------------------------------------------------------------------


async def test_answered_round_drops_off(slack, justin, ana):
    ivs = await seed_open(justin, ana)
    await chase.run_result_roundup(FRIDAY)
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
    await chase.run_result_roundup(FRIDAY)
    for iv in ivs:
        await set_status(iv, "rejected")
    await chase.refresh_latest_roundup()
    [upd] = slack.of("chat.update")
    assert upd["text"] == THANKS
    assert [b["type"] for b in upd["blocks"]] == ["section"]
    assert sections(upd) == [THANKS]


async def test_a_round_that_lost_its_caller_drops_off(slack, justin, ana):
    ivs = await seed_open(justin, ana)
    await chase.run_result_roundup(FRIDAY)
    raw = ivs[2].model_dump()["stageHistory"]
    raw[-1]["caller"]["enabled"] = False
    await Interview.find_one(Interview.id == ivs[2].id).update({"$set": {"stageHistory": raw}})
    await chase.refresh_latest_roundup()
    assert len(actions_blocks(slack.of("chat.update")[0])) == 2


async def test_a_deleted_interview_drops_off(slack, justin, ana):
    ivs = await seed_open(justin, ana)
    await chase.run_result_roundup(FRIDAY)
    await ivs[0].delete()
    await chase.refresh_latest_roundup()
    assert len(actions_blocks(slack.of("chat.update")[0])) == 2


async def test_open_rounds_carry_to_next_friday(slack, justin):
    iv = await add(justin)
    assert await chase.run_result_roundup(FRIDAY) == "posted"
    assert await chase.run_result_roundup(chi(2026, 11, 27, 18, 0)) == "posted"
    posts = slack.of("chat.postMessage")
    assert len(posts) == 2
    assert posts[1]["blocks"][-1]["elements"][0]["value"] == ref(iv)
    assert sections(posts[1])[0] == "*📋 Results still open — Fri Nov 27*"
    assert [p.friday for p in await ResultRoundupPost.find_all().sort("friday").to_list()] == [
        "2026-11-20", "2026-11-27",
    ]
    # Only the latest roundup is refreshed
    await set_status(iv, "passed")
    await chase.refresh_latest_roundup()
    [upd] = slack.of("chat.update")
    assert upd["ts"] == "9.2"


async def test_roundup_block_limit(slack, justin):
    for i in range(30):
        await add(justin, start=day(10 + i % 9, 10 + i % 5), company=f"Co{i:02d}")
    assert await chase.run_result_roundup(FRIDAY) == "posted"
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
    await chase.run_result_roundup(FRIDAY)
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


@pytest.mark.parametrize("off", [
    dict(SLACK_CALLER_ASKS_FROM=None), dict(SLACK_BOT_TOKEN=""), dict(SLACK_CALLER_CHANNEL_ID=None),
])
async def test_roundup_off_without_the_setting(slack, justin, monkeypatch, off):
    await add(justin)
    monkeypatch.setattr(chase, "get_settings", lambda: settings(**off))
    assert await chase.run_result_roundup(FRIDAY) == "off"
    await chase.refresh_latest_roundup()
    assert slack.calls == []
    assert await SlackAlertLog.find_all().to_list() == []


async def test_refresh_is_a_no_op_when_off(slack, justin, monkeypatch):
    await add(justin)
    await chase.run_result_roundup(FRIDAY)
    await set_status(await Interview.find_one({}), "passed")
    monkeypatch.setattr(chase, "get_settings", lambda: settings(SLACK_CALLER_ASKS_FROM=None))
    await chase.refresh_latest_roundup()
    assert slack.of("chat.update") == []


async def test_the_chase_runs_the_roundup_and_refreshes(slack, justin, monkeypatch):
    iv = await add(justin)
    ran = []
    real_refresh = chase.refresh_latest_roundup

    async def refresh():
        ran.append(1)
        await real_refresh()

    monkeypatch.setattr(chase, "refresh_latest_roundup", refresh)
    out = await chase.run_coordination_chase(FRIDAY)
    assert out["result_roundup"] == "posted" and ran == [1]
    assert set(out) == {"after_call_asks", "result_reminders", "result_roundup"}
    await set_status(iv, "passed")
    out = await chase.run_coordination_chase(FRIDAY + timedelta(minutes=5))
    assert out["result_roundup"] == "skipped"
    assert len(slack.of("chat.update")) == 1


async def test_a_crash_in_the_roundup_does_not_stop_the_chase(slack, justin, monkeypatch):
    monkeypatch.setattr(chase, "run_result_roundup", AsyncMock(side_effect=RuntimeError("boom")))
    refresh = AsyncMock()
    monkeypatch.setattr(chase, "refresh_latest_roundup", refresh)
    out = await chase.run_coordination_chase(FRIDAY)
    assert out["result_roundup"] == "failed"
    refresh.assert_awaited_once()


async def test_click_on_the_roundup_refreshes_it(slack, justin, ana, monkeypatch):
    monkeypatch.setattr(interviews, "sync_caller_thread", AsyncMock(return_value=False))
    monkeypatch.setattr(interviews, "refresh_today", AsyncMock(return_value=False))
    replies = []

    async def respond(url, payload):
        replies.append((url, payload))

    ivs = await seed_open(justin, ana)
    await chase.run_result_roundup(FRIDAY)
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
    await chase.run_result_roundup(FRIDAY)
    click = actions.Click("coord_result_passed", ref(ivs[0]), "U0JUSTIN01", RESPONSE_URL, "T.1", "C1", "555.5")
    with patch.object(actions, "_post_ephemeral", respond):
        await actions.process_coordination_click(click)
    assert slack.of("chat.update") == []
    assert len(replies) == 1 and replies[0][1]["replace_original"] is True
