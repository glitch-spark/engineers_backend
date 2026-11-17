"""The owner mention (pure, no Slack, no DB)."""

import os
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.slack_bot import _mention  # noqa: E402


def user(name, slack=None):
    return SimpleNamespace(name=name, email=f"{name.lower()}@x.io", slackUserId=slack)


def test_mention_without_slack_id_is_escaped_so_a_name_cannot_ping():
    assert _mention(user("<!channel> & co")) == "&lt;!channel&gt; &amp; co"
    assert _mention(user("Maria")) == "Maria"
    assert _mention(user("Ana", "U0ANA00001")) == "<@U0ANA00001>"
    assert _mention(SimpleNamespace(name="", email="<!here>@x.io", slackUserId=None)) == "&lt;!here&gt;@x.io"
