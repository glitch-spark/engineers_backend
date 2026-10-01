"""Unit tests for the 6:00 AM ET #caller reminder (no database, no Slack)."""

import asyncio
import os
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.models.interview import InterviewCaller, InterviewStageEvent  # noqa: E402
from app.slack_bot import (  # noqa: E402
    caller_digest_due,
    caller_digest_text,
    run_caller_channel_digest,
    select_caller_rounds,
)

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _iv(*rounds, company="Acme"):
    return SimpleNamespace(
        id="iv", createdBy="u1", accountId=None, companyName=company, jobUrl=None, stageHistory=list(rounds),
    )


def _round(when, status="scheduled", caller=True, stage="intro"):
    return InterviewStageEvent(
        stage=stage, at=when, scheduledAt=when.astimezone(timezone.utc), status=status,
        caller=InterviewCaller(enabled=caller, callerName="Sam", timezone="America/New_York"),
    )


def test_due_window():
    assert caller_digest_due(_et(2026, 10, 1, 5, 59)) is None
    assert caller_digest_due(_et(2026, 10, 1, 6, 0)) == date(2026, 10, 1)
    assert caller_digest_due(_et(2026, 10, 1, 11, 59)) == date(2026, 10, 1)
    assert caller_digest_due(_et(2026, 10, 1, 12, 0)) is None
    # Aware UTC input is converted to ET.
    assert caller_digest_due(datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc)) == date(2026, 10, 1)


def test_select_et_day_boundaries_and_dst():
    late_oct31 = _round(_et(2026, 10, 31, 23, 30))
    early_nov1 = _round(_et(2026, 11, 1, 0, 30))
    rows = select_caller_rounds([_iv(late_oct31, early_nov1)], date(2026, 11, 1))
    assert [r[1] for r in rows] == [early_nov1]

    late_mar7 = _round(_et(2026, 3, 7, 23, 30))
    early_mar8 = _round(_et(2026, 3, 8, 0, 30))
    rows = select_caller_rounds([_iv(late_mar7, early_mar8)], date(2026, 3, 8))
    assert [r[1] for r in rows] == [early_mar8]


def test_select_only_scheduled_caller_rounds():
    day = date(2026, 10, 1)
    keep = _round(_et(2026, 10, 1, 10))
    rows = select_caller_rounds([
        _iv(_round(_et(2026, 10, 1, 9), status="canceled")),
        _iv(_round(_et(2026, 10, 1, 9), status="completed")),
        _iv(_round(_et(2026, 10, 1, 9), caller=False)),
        _iv(keep),
    ], day)
    assert [r[1] for r in rows] == [keep]


def test_select_sorted_by_start():
    a = _round(_et(2026, 10, 1, 15))
    b = _round(_et(2026, 10, 1, 9), stage="panel")
    rows = select_caller_rounds([_iv(a), _iv(b)], date(2026, 10, 1))
    assert [r[1] for r in rows] == [b, a]


class _Fakes:
    def __init__(self, *, post_ok=True, interviews=()):
        self.claimed: set[str] = set()
        self.posts: list[str] = []
        self.released: list[str] = []
        self.post_ok = post_ok
        self.interviews = list(interviews)

    async def load(self, start, end):
        return self.interviews

    async def claim(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    async def release(self, key):
        self.released.append(key)
        self.claimed.discard(key)

    async def post(self, text):
        self.posts.append(text)
        return self.post_ok

    async def labels(self, rows):
        return {}, {}


def _run(now, fakes):
    return asyncio.run(run_caller_channel_digest(
        now, load=fakes.load, claim=fakes.claim, release=fakes.release, post=fakes.post,
        labels=fakes.labels, configured=lambda: True,
    ))


def test_digest_posts_once():
    fakes = _Fakes()
    assert _run(_et(2026, 10, 1, 6, 5), fakes) == "posted"
    assert _run(_et(2026, 10, 1, 6, 10), fakes) == "already_sent"
    assert len(fakes.posts) == 1


def test_digest_not_due_outside_window():
    fakes = _Fakes()
    assert _run(_et(2026, 10, 1, 5, 0), fakes) == "not_due"
    assert fakes.posts == []


def test_digest_releases_claim_on_failure():
    fakes = _Fakes(post_ok=False)
    assert _run(_et(2026, 10, 1, 6, 5), fakes) == "post_failed"
    assert fakes.released == ["caller-digest:2026-10-01"]
    fakes.post_ok = True
    assert _run(_et(2026, 10, 1, 6, 10), fakes) == "posted"


def test_empty_day_posts_no_interviews():
    fakes = _Fakes()
    _run(_et(2026, 10, 1, 6, 5), fakes)
    assert "No caller interviews today" in fakes.posts[0]


def test_digest_text_lists_rounds():
    r = _round(_et(2026, 10, 1, 10, 30))
    text = caller_digest_text([(_iv(r, company="Globex"), r)], profiles={}, owners={"u1": "<@U1>"})
    assert "Caller interviews today — 1" in text
    assert "10:30 AM" in text and "Globex" in text and "<@U1>" in text and "Sam" in text
