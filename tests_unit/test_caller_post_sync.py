"""One #caller thread per interview: sync_caller_thread with Slack faked (no network, no DB)."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app import slack_bot  # noqa: E402
from app.interview_caller import round_caller_snapshot  # noqa: E402
from app.models.interview import InterviewCaller, InterviewStageEvent, SlackThread  # noqa: E402

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
LATER = START + timedelta(days=1)
OWNER = SimpleNamespace(name="Pedro", email="p@x.io", slackUserId="U0OWNER001")
MANAGER = SimpleNamespace(name="Maria", email="m@x.io", slackUserId="U0MANAGER1")


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
    return {e.id: round_caller_snapshot(e) for e in interview.stageHistory}


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
        patch.object(slack_bot, "_main_post_text", AsyncMock(return_value="MAIN")),
        patch.object(slack_bot, "_owner_and_managers", AsyncMock(return_value=(OWNER, [MANAGER]))),
    ):
        changed = asyncio.run(slack_bot.sync_caller_thread(interview, before, **kw))
    return changed, calls


THREAD = SlackThread(channel="C1", ts="111.222")


def test_first_caller_round_creates_thread():
    interview = iv(rnd("r1"))
    changed, calls = run(interview, {})
    assert changed is True
    assert [c[0] for c in calls] == ["chat.postMessage"] and "thread_ts" not in calls[0][1]
    assert (interview.slackThread.channel, interview.slackThread.ts) == ("C1", "999.000")


def test_no_caller_round_posts_nothing():
    interview = iv(rnd("r1", caller=False))
    assert run(interview, {}) == (False, [])
    assert interview.slackThread is None


def test_canceled_first_round_posts_nothing():
    assert run(iv(rnd("r1", status="canceled")), {})[1] == []


def test_next_round_replies_moved_in_same_thread_and_updates_main():
    first = rnd("r1", status="passed")
    interview = iv(first, rnd("r2", at=LATER, stage="tech_round_1"), thread=THREAD)
    before = {"r1": round_caller_snapshot(rnd("r1"))}
    changed, calls = run(interview, before, added={"r2"})
    assert changed is False
    assert calls[0] == ("chat.update", {"channel": "C1", "ts": "111.222", "text": "MAIN"})
    assert [c[0] for c in calls] == ["chat.update", "chat.postMessage"]
    reply = calls[1][1]
    assert reply["thread_ts"] == "111.222" and reply["text"].startswith(":arrow_right: <@U0OWNER001> — *Moved to Tech Round 1*")


def test_next_round_with_mark_previous_passed_posts_one_reply():
    interview = iv(rnd("r1", status="passed", name="Dana"), rnd("r2", at=LATER, stage="final"), thread=THREAD)
    _, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1"))}, added={"r2"})
    assert [c[0] for c in calls].count("chat.postMessage") == 1


def test_confirm_replies_confirmed():
    interview = iv(rnd("r1", confirmed=True), thread=THREAD)
    _, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1"))})
    text = calls[1][1]["text"]
    assert text.startswith(":white_check_mark: <@U0OWNER001> — *Confirmed*")
    assert text.splitlines()[-1] == "cc <@U0MANAGER1>"


def test_reschedule_replies_rescheduled():
    interview = iv(rnd("r1", at=LATER), thread=THREAD)
    _, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1", confirmed=True))})
    assert calls[1][1]["text"].startswith(":repeat: <@U0OWNER001> — *Rescheduled*")


def test_cancel_replies_canceled():
    interview = iv(rnd("r1", status="canceled"), thread=THREAD)
    _, calls = run(interview, snaps(iv(rnd("r1"))))
    assert [c[0] for c in calls] == ["chat.update", "chat.postMessage"]
    assert calls[1][1]["text"].startswith(":no_entry: <@U0OWNER001> — *Canceled*")


def test_unchanged_write_does_nothing():
    interview = iv(rnd("r1"), thread=THREAD)
    assert run(interview, snaps(interview)) == (False, [])


def test_caller_turned_off_updates_main_post_without_reply():
    interview = iv(rnd("r1", caller=False), thread=THREAD)
    _, calls = run(interview, snaps(iv(rnd("r1"))))
    assert [c[0] for c in calls] == ["chat.update"]


def test_old_round_post_is_adopted_as_thread():
    interview = iv(rnd("r1", legacy_ts="555.000", status="passed"), rnd("r2", at=LATER, stage="final"))
    changed, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1", legacy_ts="555.000"))}, added={"r2"})
    assert changed is True and (interview.slackThread.channel, interview.slackThread.ts) == ("C1", "555.000")
    assert [c[0] for c in calls] == ["chat.update", "chat.postMessage"]
    assert calls[0][1]["ts"] == "555.000" and calls[1][1]["thread_ts"] == "555.000"


def test_gone_main_post_is_reposted():
    interview = iv(rnd("r1", confirmed=True), thread=THREAD)
    changed, calls = run(interview, {"r1": round_caller_snapshot(rnd("r1"))}, fail=("chat.update", "message_not_found"))
    assert changed is True and interview.slackThread.ts == "999.000"
    assert [c[0] for c in calls] == ["chat.update", "chat.postMessage"] and "thread_ts" not in calls[1][1]


def test_round_deleted_replies_removed_and_describes_remaining_round():
    interview = iv(rnd("r1"), rnd("r2", at=LATER, stage="panel"), thread=THREAD)
    _, calls = run(interview, snaps(interview), deleted={"r2"})
    assert [c[0] for c in calls] == ["chat.update", "chat.postMessage"]
    assert calls[1][1]["text"].startswith(":wastebasket: <@U0OWNER001> — *Round removed* — Panel")


def test_interview_delete_removes_main_post_and_old_round_posts():
    interview = iv(rnd("r1", legacy_ts="555.000"), rnd("r2", at=LATER), thread=THREAD)
    _, calls = run(interview, snaps(interview), deleted={"r1", "r2"})
    assert sorted(c[1]["ts"] for c in calls if c[0] == "chat.delete") == ["111.222", "555.000"]


def test_slack_error_never_raises():
    interview = iv(rnd("r1", confirmed=True), thread=THREAD)
    changed, _ = run(interview, {"r1": round_caller_snapshot(rnd("r1"))}, fail=("chat.update", "ratelimited"))
    assert changed is False


# --- the 30-minute reminder follows the interview thread ---------------------------------------


def _remind(interview, entry):
    calls = []

    async def api(method, body):
        calls.append((method, body))
        return {"ok": True}

    with (
        patch.object(slack_bot, "_slack_api", api),
        patch.object(slack_bot, "get_settings", lambda: SimpleNamespace(SLACK_CALLER_CHANNEL_ID="C1")),
    ):
        assert asyncio.run(slack_bot._reminder_post(interview, entry, "soon")) is True
    return calls


def test_reminder_posts_in_interview_thread():
    entry = rnd("r2", legacy_ts="555.000")
    calls = _remind(iv(rnd("r1"), entry, thread=SlackThread(channel="C9", ts="111.222")), entry)
    assert (calls[0][1]["channel"], calls[0][1]["thread_ts"]) == ("C9", "111.222")


def test_reminder_falls_back_to_round_post():
    entry = rnd("r1", legacy_ts="555.000")
    calls = _remind(iv(entry), entry)
    assert (calls[0][1]["channel"], calls[0][1]["thread_ts"]) == ("C1", "555.000")
