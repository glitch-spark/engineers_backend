"""Unit tests for keeping a round's #caller main post in sync (Slack faked)."""

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app import slack_bot  # noqa: E402
from app.interview_caller import round_caller_snapshot  # noqa: E402
from app.models.interview import InterviewCaller, InterviewStageEvent  # noqa: E402

START = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def _entry(ts="111.222", status="scheduled", **caller):
    return InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, status=status,
        caller=InterviewCaller(enabled=True, startsAt=START, slackChannelTs=ts, **caller),
    )


def _run(entry, kind, *, prev=None, fail=None):
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
        patch.object(slack_bot.UserExtra, "get", AsyncMock(return_value=None)),
    ):
        iv = SimpleNamespace(createdBy="u1", accountId=None, companyName="Acme")
        ts = asyncio.run(slack_bot.notify_round_caller(iv, entry, kind, prev=prev))
    return ts, calls


def test_update_edits_main_post_and_replies_in_thread():
    prev = round_caller_snapshot(_entry(callerName="TBD"))
    ts, calls = _run(_entry(callerName="Dana"), "updated", prev=prev)
    assert ts == "111.222"
    assert calls[0] == ("chat.update", {"channel": "C1", "ts": "111.222", "text": "MAIN"})
    method, body = calls[1]
    assert method == "chat.postMessage" and body["thread_ts"] == "111.222"
    assert "Caller assigned: TBD → Dana" in body["text"]


def test_cancel_edits_main_post_and_replies():
    prev = round_caller_snapshot(_entry())
    ts, calls = _run(_entry(status="canceled"), "canceled", prev=prev)
    assert ts == "111.222"
    assert [c[0] for c in calls] == ["chat.update", "chat.postMessage"]
    assert calls[1][1]["text"].startswith(":no_entry: *Canceled*")


def test_restore_replies_back_on():
    prev = round_caller_snapshot(_entry(status="canceled"))
    _, calls = _run(_entry(), "restored", prev=prev)
    assert calls[1][1]["text"].startswith(":white_check_mark: *Back on*")
    assert "Status: Canceled → Scheduled" in calls[1][1]["text"]


def test_delete_removes_main_post():
    ts, calls = _run(_entry(), "deleted")
    assert ts is None
    assert calls == [("chat.delete", {"channel": "C1", "ts": "111.222"})]


def test_main_post_gone_is_posted_again():
    prev = round_caller_snapshot(_entry(callerName="TBD"))
    ts, calls = _run(_entry(callerName="Dana"), "updated", prev=prev, fail=("chat.update", "message_not_found"))
    assert ts == "999.000"
    assert [c[0] for c in calls] == ["chat.update", "chat.postMessage"]
    assert "thread_ts" not in calls[1][1]


def test_cancel_with_main_post_gone_posts_nothing():
    _, calls = _run(_entry(status="canceled"), "canceled", fail=("chat.update", "message_not_found"))
    assert [c[0] for c in calls] == ["chat.update"]
