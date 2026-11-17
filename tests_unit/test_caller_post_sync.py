"""One #caller thread per interview: sync_caller_thread with Slack faked (no network; the DB is an in-memory mongomock)."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app import slack_bot  # noqa: E402
from app.interview_caller import round_caller_snapshot, round_post_state  # noqa: E402
from app.models import UserExtra  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.caller import Caller  # noqa: E402
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent, SlackThread  # noqa: E402
from app.models.interview_question import InterviewQuestion  # noqa: E402

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
LATER = START + timedelta(days=1)
BLOCKS = [{"type": "section", "text": {"type": "mrkdwn", "text": "MAIN"}}]


def rnd(rid, *, at=START, status="scheduled", confirmed=False, caller=True, stage="intro", legacy_ts=None, name="Sam"):
    return InterviewStageEvent(
        id=rid, stage=stage, at=at, scheduledAt=at, status=status, confirmed=confirmed,
        caller=InterviewCaller(enabled=caller, callerName=name, startsAt=at, method="video", slackChannelTs=legacy_ts),
    )


def iv(*rounds, thread=None):
    return SimpleNamespace(
        id="iv1", createdBy="u1", accountId=None, companyName="Acme", jobUrl=None,
        stageHistory=list(rounds), slackThread=thread,
    )


def snaps(interview):
    return {e.id: round_post_state(e) for e in interview.stageHistory}


def run(interview, before, *, fail=None, **kw):
    calls = []

    async def api(method, body):
        calls.append((method, body))
        if fail and method == fail[0]:
            raise RuntimeError(fail[1])
        return {"ok": True, "ts": "999.000"}

    with (
        patch.object(slack_bot, "_slack_api", api),
        patch.object(slack_bot, "slack_bot_configured", lambda: True),
        patch.object(slack_bot, "get_settings", lambda: SimpleNamespace(SLACK_CALLER_CHANNEL_ID="C1")),
        patch.object(slack_bot, "_main_post", AsyncMock(return_value=("MAIN", BLOCKS))),
    ):
        changed = asyncio.run(slack_bot.sync_caller_thread(interview, before, **kw))
    return changed, calls


THREAD = SlackThread(channel="C1", ts="111.222")


def test_first_caller_round_creates_thread():
    interview = iv(rnd("r1"))
    changed, calls = run(interview, {})
    assert changed is True
    assert [c[0] for c in calls] == ["chat.postMessage"] and "thread_ts" not in calls[0][1]
    assert calls[0][1]["text"] == "MAIN" and calls[0][1]["blocks"] == BLOCKS
    assert (interview.slackThread.channel, interview.slackThread.ts) == ("C1", "999.000")


def test_no_caller_round_posts_nothing():
    interview = iv(rnd("r1", caller=False))
    assert run(interview, {}) == (False, [])
    assert interview.slackThread is None


def test_canceled_first_round_posts_nothing():
    assert run(iv(rnd("r1", status="canceled")), {})[1] == []


def only_update(calls):
    assert [c[0] for c in calls] == ["chat.update"]
    assert calls[0][1]["blocks"] == BLOCKS and calls[0][1]["text"] == "MAIN"
    assert set(calls[0][1]) == {"channel", "ts", "text", "blocks"}


def test_next_round_edits_the_post_without_a_reply():
    first = rnd("r1", status="passed")
    interview = iv(first, rnd("r2", at=LATER, stage="tech_round_1"), thread=THREAD)
    before = {"r1": round_caller_snapshot(rnd("r1"))}
    changed, calls = run(interview, before, added={"r2"})
    assert changed is False
    only_update(calls)
    assert calls[0][1]["channel"] == "C1" and calls[0][1]["ts"] == "111.222"


def test_next_round_with_mark_previous_passed_edits_the_post_without_a_reply():
    interview = iv(rnd("r1", status="passed", name="Dana"), rnd("r2", at=LATER, stage="final"), thread=THREAD)
    _, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1"))}, added={"r2"})
    only_update(calls)


def test_confirm_edits_the_post_without_a_reply():
    interview = iv(rnd("r1", confirmed=True), thread=THREAD)
    _, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1"))})
    only_update(calls)


def test_reschedule_edits_the_post_without_a_reply():
    interview = iv(rnd("r1", at=LATER), thread=THREAD)
    _, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1", confirmed=True))})
    only_update(calls)


def test_cancel_edits_the_post_without_a_reply():
    interview = iv(rnd("r1", status="canceled"), thread=THREAD)
    _, calls = run(interview, snaps(iv(rnd("r1"))))
    only_update(calls)


def test_unchanged_write_does_nothing():
    interview = iv(rnd("r1"), thread=THREAD)
    assert run(interview, snaps(interview)) == (False, [])


def test_caller_turned_off_updates_main_post_without_reply():
    interview = iv(rnd("r1", caller=False), thread=THREAD)
    _, calls = run(interview, snaps(iv(rnd("r1"))))
    only_update(calls)


def test_status_change_on_a_round_with_no_caller_edits_the_post():
    """The caller is off, so the snapshot is None both times; the round's status still re-renders the post."""
    interview = iv(rnd("r1", caller=False, status="completed"), thread=THREAD)
    _, calls = run(interview, snaps(iv(rnd("r1", caller=False))))
    only_update(calls)


def test_unchanged_round_with_no_caller_does_nothing():
    interview = iv(rnd("r1", caller=False), thread=THREAD)
    assert run(interview, snaps(interview)) == (False, [])


def test_status_change_with_no_caller_and_no_post_posts_nothing():
    interview = iv(rnd("r1", caller=False, status="completed"))
    assert run(interview, snaps(iv(rnd("r1", caller=False)))) == (False, [])
    assert interview.slackThread is None


def test_old_round_post_is_adopted_as_thread():
    interview = iv(rnd("r1", legacy_ts="555.000", status="passed"), rnd("r2", at=LATER, stage="final"))
    changed, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1", legacy_ts="555.000"))}, added={"r2"})
    assert changed is True and (interview.slackThread.channel, interview.slackThread.ts) == ("C1", "555.000")
    only_update(calls)
    assert calls[0][1]["ts"] == "555.000"


def test_gone_main_post_is_reposted():
    interview = iv(rnd("r1", confirmed=True), thread=THREAD)
    changed, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1"))}, fail=("chat.update", "message_not_found"))
    assert changed is True and interview.slackThread.ts == "999.000"
    assert [c[0] for c in calls] == ["chat.update", "chat.postMessage"] and "thread_ts" not in calls[1][1]
    assert calls[1][1]["text"] == "MAIN" and calls[1][1]["blocks"] == BLOCKS


def test_round_deleted_edits_the_post_without_a_reply():
    interview = iv(rnd("r1"), rnd("r2", at=LATER, stage="panel"), thread=THREAD)
    _, calls = run(interview, snaps(interview), deleted={"r2"})
    only_update(calls)


def test_interview_delete_removes_main_post_and_old_round_posts():
    interview = iv(rnd("r1", legacy_ts="555.000"), rnd("r2", at=LATER), thread=THREAD)
    _, calls = run(interview, snaps(interview), deleted={"r1", "r2"})
    assert sorted(c[1]["ts"] for c in calls if c[0] == "chat.delete") == ["111.222", "555.000"]


def test_slack_error_never_raises():
    interview = iv(rnd("r1", confirmed=True), thread=THREAD)
    changed, _ = run(interview, {"r1": round_caller_snapshot(rnd("r1"))}, fail=("chat.update", "ratelimited"))
    assert changed is False


# --- the real _main_post (mongomock) ------------------------------------------------------------


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["post-sync-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion, Caller])
    return database


@pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")
@pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning")
async def test_main_post_describes_the_latest_round(db):
    owner = await UserExtra(name="Pedro", email="p@x.io", role="staff", slackUserId="U0OWNER001").insert()
    cal = await UserExtra(name="Cal", email="c@x.io", role="staff").insert()

    def caller():
        return InterviewCaller(enabled=True, callerId=str(cal.id), callerName="Cal", callerKind="user",
                               startsAt=START, method="video")

    first = InterviewStageEvent(id="a" * 32, stage="intro", at=START, scheduledAt=START, status="scheduled",
                                confirmed=True, caller=caller())
    second = InterviewStageEvent(id="b" * 32, stage="final", at=LATER, scheduledAt=LATER, status="scheduled",
                                 caller=caller())
    interview = await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[first.model_dump(), second.model_dump()], stage="final", status="scheduled",
    ).insert()

    text, blocks = await slack_bot._main_post(interview, interview.stageHistory[-1])

    assert "Acme" in text
    buttons = {e.get("action_id"): e for e in blocks[1]["elements"]}
    assert buttons["coord_confirm"]["value"] == f"{interview.id}:{second.id}"
    select = buttons["coord_choose_caller"]
    values = [o["value"] for g in select["option_groups"] for o in g["options"]]
    assert values and all(v.startswith(f"{interview.id}:{second.id}:") for v in values)
