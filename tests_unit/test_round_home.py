"""A round's home message in the daily channel: follow-ups thread under it, or create it (Slack faked)."""

import os
from datetime import datetime, timedelta, timezone
from itertools import count

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import slack_bot
from app.interview_day import home
from app.models.round_slack_post import RoundSlackPost

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")

IV, RND = "a" * 24, "b" * 32
START = datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc)
BLOCKS = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]


class Slack:
    def __init__(self):
        self.calls, self.errors, self._n = [], [], count(1)

    async def __call__(self, method, body):
        self.calls.append((method, body))
        if self.errors and (err := self.errors.pop(0)):
            raise RuntimeError(err)
        return {"ok": True, "ts": f"5.{next(self._n)}"}

    def posts(self):
        return [b for m, b in self.calls if m == "chat.postMessage"]


@pytest.fixture
async def slack(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["round-home-unit"], document_models=[RoundSlackPost])
    fake = Slack()
    monkeypatch.setattr(slack_bot, "_slack_api", fake)
    return fake


async def _homes():
    return [(h.roundId, h.startEpoch, h.channel, h.ts) for h in await RoundSlackPost.find_all().to_list()]


async def test_no_home_posts_top_level_and_records_it(slack):
    ts = await home.post_in_home(RND, IV, START, "C_DAILY", "hi", BLOCKS)
    (post,) = slack.posts()
    assert "thread_ts" not in post and post["channel"] == "C_DAILY" and post["blocks"] == BLOCKS
    assert ts == "5.1"
    assert await _homes() == [(RND, home.start_epoch(START), "C_DAILY", "5.1")]


async def test_home_exists_replies_in_its_thread(slack):
    await home.record_home(RND, IV, START, "C_DAILY", "1.1")
    ts = await home.post_in_home(RND, IV, START, "C_DAILY", "hi", BLOCKS)
    assert slack.posts()[0]["thread_ts"] == "1.1" and ts == "5.1"
    assert await _homes() == [(RND, home.start_epoch(START), "C_DAILY", "1.1")]


async def test_gone_home_posts_top_level_and_replaces_it(slack):
    await home.record_home(RND, IV, START, "C_DAILY", "1.1")
    slack.errors = ["thread_not_found"]
    await home.post_in_home(RND, IV, START, "C_DAILY", "hi", BLOCKS)
    first, second = slack.posts()
    assert first["thread_ts"] == "1.1" and "thread_ts" not in second
    assert await _homes() == [(RND, home.start_epoch(START), "C_DAILY", "5.1")]


async def test_other_slack_errors_raise(slack):
    slack.errors = ["ratelimited"]
    with pytest.raises(RuntimeError):
        await home.post_in_home(RND, IV, START, "C_DAILY", "hi", BLOCKS)


async def test_home_is_per_start(slack):
    await home.record_home(RND, IV, START, "C_DAILY", "1.1")
    moved = START + timedelta(days=1)
    await home.post_in_home(RND, IV, moved, "C_DAILY", "hi", BLOCKS)
    assert "thread_ts" not in slack.posts()[0]
    assert sorted(await _homes()) == sorted([(RND, home.start_epoch(START), "C_DAILY", "1.1"),
                                             (RND, home.start_epoch(moved), "C_DAILY", "5.1")])


async def test_record_home_twice_keeps_one_row(slack):
    await home.record_home(RND, IV, START, "C_DAILY", "1.1")
    await home.record_home(RND, IV, START, "C_DAILY", "2.2")
    assert await _homes() == [(RND, home.start_epoch(START), "C_DAILY", "2.2")]
