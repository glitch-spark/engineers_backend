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


OWNER, MANAGER = _user("Ann", "UA"), _user("Ben", "UB")
ACCOUNT = SimpleNamespace(name="Ian Meron")


def test_reminder_text_tags_owner_and_managers():
    r = _round(27, stage="tech")
    text = caller_reminder_text(_iv(r, company="Globex"), r, now=NOW, owner=OWNER, managers=[MANAGER], account=ACCOUNT)
    assert text.splitlines() == [
        f":alarm_clock: *Starting in 27 min* — {slack_time(r.scheduledAt, with_day=False)} · Tech · Globex",
        "*Profile:* Ian Meron",
        "*Method:* Video meeting — <https://meet.example/x|https://meet.example/x>",
        "*Caller assigned:* Sam",
        "cc <@UA> <@UB>",
    ]


def test_reminder_text_without_account_or_managers():
    r = _round(27)
    text = caller_reminder_text(_iv(r), r, now=NOW, owner=OWNER, managers=[], account=None)
    assert "*Profile:* —" in text and text.splitlines()[-1] == "cc <@UA>"


def test_reminder_cc_lists_the_owner_once_when_also_a_manager():
    r = _round(27)
    text = caller_reminder_text(_iv(r), r, now=NOW, owner=OWNER, managers=[OWNER, MANAGER], account=None)
    assert text.splitlines()[-1] == "cc <@UA> <@UB>"


def test_reminder_without_anyone_to_cc_has_no_cc_line():
    r = _round(27)
    text = caller_reminder_text(_iv(r), r, now=NOW, owner=None, managers=[], account=None)
    assert "cc " not in text and text.splitlines()[-1] == "*Caller assigned:* Sam"


def test_reminder_text_escapes_user_entered_text():
    r = _round(27)
    r.caller.callerName = "<@U123> Sam"
    text = caller_reminder_text(
        _iv(r, company="Acme <!channel> & Co"), r, now=NOW, owner=OWNER, managers=[MANAGER],
        account=SimpleNamespace(name="<!here> Profile"),
    )
    lines = text.splitlines()
    assert lines[0].endswith(" · Intro · Acme &lt;!channel&gt; &amp; Co")
    assert "*Profile:* &lt;!here&gt; Profile" in lines
    assert "*Caller assigned:* &lt;@U123&gt; Sam" in lines
    assert "<!channel>" not in text and "<!here>" not in text and "<@U123>" not in text
    assert lines[-1] == "cc <@UA> <@UB>" and "<!date^" in lines[0]


def test_reminder_text_escapes_a_method_value_that_is_not_a_link():
    r = _round(27)
    r.caller.methodValue = "dial <!channel> & hold"
    text = caller_reminder_text(_iv(r), r, now=NOW, owner=OWNER, managers=[], account=None)
    assert "*Method:* Video meeting — dial &lt;!channel&gt; &amp; hold" in text.splitlines()


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
        return OWNER, [MANAGER], ACCOUNT


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


def test_reminder_run_posts_profile_and_cc_from_people():
    fakes = _Fakes([_iv(_round(28))])
    assert _run(fakes) == {"sent": 1, "failed": 0}
    lines = fakes.posts[0][1].splitlines()
    assert lines[1] == "*Profile:* Ian Meron" and lines[-1] == "cc <@UA> <@UB>"


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


# --- who the reminder tags (mongomock) ----------------------------------------------------------


async def test_reminder_people_are_owner_managers_and_account():
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    from app import slack_bot
    from app.models import Account, UserExtra

    await init_beanie(database=AsyncMongoMockClient()["reminder-unit"], document_models=[Account, UserExtra])
    owner = await UserExtra(name="Ann", email="ann@x.io", slackUserId="UA").insert()
    await UserExtra(name="Zed", email="zed@x.io", role="interview_manager").insert()
    await UserExtra(name="Ben", email="ben@x.io", role="interview_manager").insert()
    account = await Account(name="Ian Meron", createdBy=owner.id).insert()
    iv = SimpleNamespace(createdBy=owner.id, accountId=account.id)

    got_owner, managers, got_account = await slack_bot._reminder_people(iv, None)
    assert got_owner.id == owner.id and [m.name for m in managers] == ["Ben", "Zed"]
    assert got_account.name == "Ian Meron"

    got_owner, _, got_account = await slack_bot._reminder_people(SimpleNamespace(createdBy=owner.id, accountId=None), None)
    assert got_owner.id == owner.id and got_account is None
