"""Unit tests for the 30-minute #caller thread reminder (no database, no Slack)."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.models.interview import InterviewCaller, InterviewStageEvent  # noqa: E402
from app.slack_bot import (  # noqa: E402
    caller_reminder_key,
    caller_reminder_text,
    due_caller_reminders,
    run_caller_reminders,
    slack_time,
)

NOW = datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc)


def _iv(*rounds, company="Acme"):
    return SimpleNamespace(id="iv", createdBy="u1", accountId=None, companyName=company, stageHistory=list(rounds))


def _round(minutes, status="scheduled", caller=True, stage="intro"):
    when = NOW + timedelta(minutes=minutes)
    return InterviewStageEvent(
        stage=stage, at=when, scheduledAt=when, status=status,
        caller=InterviewCaller(enabled=caller, callerName="Sam", startsAt=when, method="video",
                               methodValue="https://meet.example/x"),
    )


def test_due_within_the_next_30_minutes():
    edge, soon, later, started = _round(30), _round(12), _round(31), _round(0)
    rows = due_caller_reminders([_iv(later, started), _iv(edge, soon)], NOW)
    assert [r[1] for r in rows] == [soon, edge]  # sorted by start


def test_due_only_scheduled_rounds_with_a_caller():
    keep = _round(20)
    rows = due_caller_reminders([
        _iv(_round(20, status="canceled")),
        _iv(_round(20, status="rejected")),
        _iv(_round(20, status="completed")),
        _iv(_round(20, caller=False)),
        _iv(keep),
    ], NOW)
    assert [r[1] for r in rows] == [keep]


def test_naive_start_from_mongo_is_utc():
    r = _round(20)
    r.scheduledAt = r.scheduledAt.replace(tzinfo=None)
    assert [row[1] for row in due_caller_reminders([_iv(r)], NOW)] == [r]


def test_key_changes_when_the_round_moves():
    r = _round(20)
    key = caller_reminder_key(r)
    assert key.startswith(f"caller-reminder:{r.id}:")
    r.scheduledAt = r.scheduledAt + timedelta(hours=1)
    assert caller_reminder_key(r) != key


def _user(name, slack=None):
    return SimpleNamespace(name=name, email=f"{name}@x.com", slackUserId=slack)


def test_reminder_text_tags_coworkers_and_owner():
    r = _round(27, stage="tech")
    text = caller_reminder_text(
        _iv(r, company="Globex"), r, now=NOW,
        owner=_user("Ann", "UA"), coworkers=[_user("Bo", "UB"), _user("Casey")],
    )
    assert text.startswith(":alarm_clock: *Starting in 27 min* — ")
    assert slack_time(r.scheduledAt, with_day=False) in text
    assert "Globex" in text and "https://meet.example/x" in text and "Sam" in text
    assert text.splitlines()[-1] == "<@UB> Casey <@UA>"


class _Fakes:
    def __init__(self, interviews=(), post_ok=True):
        self.interviews = list(interviews)
        self.post_ok = post_ok
        self.claimed: set[str] = set()
        self.released: list[str] = []
        self.posts: list[tuple] = []

    async def load(self, start, end):
        assert start.tzinfo is None and end - start == timedelta(minutes=30)
        return self.interviews

    async def claim(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    async def release(self, key):
        self.released.append(key)
        self.claimed.discard(key)

    async def post(self, iv, entry, text):
        self.posts.append((entry.id, text))
        return self.post_ok

    async def people(self, iv, entry):
        return _user("Ann", "UA"), []


def _run(fakes, now=NOW):
    return asyncio.run(run_caller_reminders(
        now, load=fakes.load, claim=fakes.claim, release=fakes.release, post=fakes.post,
        people=fakes.people, configured=lambda: True,
    ))


def test_reminder_sent_once_per_start_time():
    r = _round(28)
    fakes = _Fakes([_iv(r)])
    assert _run(fakes) == {"sent": 1, "failed": 0}
    assert _run(fakes, NOW + timedelta(minutes=5)) == {"sent": 0, "failed": 0}
    assert len(fakes.posts) == 1
    r.scheduledAt = r.scheduledAt + timedelta(minutes=10)  # rescheduled → a fresh reminder
    assert _run(fakes, NOW + timedelta(minutes=10)) == {"sent": 1, "failed": 0}


def test_failed_post_releases_the_claim():
    r = _round(28)
    fakes = _Fakes([_iv(r)], post_ok=False)
    assert _run(fakes) == {"sent": 0, "failed": 1}
    assert fakes.released == [caller_reminder_key(r)]
    fakes.post_ok = True
    assert _run(fakes) == {"sent": 1, "failed": 0}


def test_nothing_when_slack_is_off():
    fakes = _Fakes([_iv(_round(20))])
    res = asyncio.run(run_caller_reminders(
        NOW, load=fakes.load, claim=fakes.claim, release=fakes.release, post=fakes.post,
        people=fakes.people, configured=lambda: False,
    ))
    assert res == {"sent": 0, "failed": 0}
    assert fakes.posts == []
