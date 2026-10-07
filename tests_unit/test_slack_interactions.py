"""Approve / Reject buttons in Slack: the signature check, then POST /integrations/slack/interactions with
signed requests, Slack and the response_url faked, against an in-memory MongoDB. No network, no real DB."""

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app import slack_transactions as st  # noqa: E402
from app.models import Transaction, UserExtra  # noqa: E402
from app.routers import slack_bot as slack_router  # noqa: E402
from app.slack_transactions import handle_transaction_action, verify_slack_signature  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")

SECRET = "test-signing-secret"
RESPONSE_URL = "https://hooks.slack.com/actions/T1/1/abc"
PATH = "/integrations/slack/interactions"


# --- signature --------------------------------------------------------------------------------------


def test_signature_valid_wrong_secret_stale_and_tampered():
    body, ts = b"payload=%7B%7D", "1760000000"
    sig = "v0=" + hmac.new(b"s3cret", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    assert verify_slack_signature("s3cret", ts, body, sig, now=1760000060) is True
    assert verify_slack_signature("other", ts, body, sig, now=1760000060) is False
    assert verify_slack_signature("s3cret", ts, body, sig, now=1760000000 + 301) is False
    assert verify_slack_signature("s3cret", ts, body + b"x", sig, now=1760000060) is False


def test_signature_rejects_future_garbage_and_missing_values():
    body, ts = b"payload=%7B%7D", "1760000000"
    sig = "v0=" + hmac.new(b"s3cret", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    assert verify_slack_signature("s3cret", ts, body, sig, now=1760000000 - 301) is False  # from the future
    assert verify_slack_signature("s3cret", "not-a-number", body, sig, now=1760000060) is False
    assert verify_slack_signature("s3cret", "", body, sig, now=1760000060) is False
    assert verify_slack_signature("s3cret", ts, body, "", now=1760000060) is False
    assert verify_slack_signature("", ts, body, sig, now=1760000060) is False


# --- fixtures ---------------------------------------------------------------------------------------


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["slack-interactions-unit"]
    await init_beanie(database=database, document_models=[Transaction, UserExtra])
    return database


class FakeSlack(list):
    """Recorded Slack Web API calls (method, body) and ephemeral replies (url, json)."""

    def __init__(self):
        super().__init__()
        self.ephemeral: list[tuple[str, dict]] = []

    async def api(self, method, body):
        self.append((method, body))
        return {"ok": True, "ts": "900.001", "channel": body.get("channel")}

    async def respond(self, url, payload):
        self.ephemeral.append((url, payload))

    def texts(self):
        return [p["text"] for _, p in self.ephemeral]


def settings(secret=SECRET):
    return lambda: SimpleNamespace(SLACK_TRANSACTIONS_CHANNEL_ID="C-TX", SLACK_SIGNING_SECRET=secret)


@pytest.fixture
def slack():
    fake = FakeSlack()
    with (
        patch.object(st, "_slack_api", fake.api),
        patch.object(st, "_post_ephemeral", fake.respond),
        patch.object(st, "slack_bot_configured", lambda: True),
        patch.object(st, "get_settings", settings()),
        patch.object(slack_router, "get_settings", settings()),
    ):
        yield fake


@pytest.fixture
def client(db, slack):
    """A client with NO auth override: the route must not need the app's login."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.include_router(slack_router.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def signed(body: bytes, secret=SECRET, ts=None) -> dict:
    ts = ts or str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def click_body(action_id, value, slack_user="U0ADMIN001", kind="block_actions") -> bytes:
    payload = {
        "type": kind, "user": {"id": slack_user}, "response_url": RESPONSE_URL,
        "actions": [{"action_id": action_id, "value": value, "type": "button"}],
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


async def click(c, action_id, value, slack_user="U0ADMIN001"):
    body = click_body(action_id, value, slack_user)
    return await c.post(PATH, content=body, headers=signed(body))


async def make_user(name, role="staff", slack_id=None, active=True) -> UserExtra:
    return await UserExtra(
        name=name, email=f"{name.lower()}@x.io", role=role, isActive=active, slackUserId=slack_id
    ).insert()


async def make_tx(owner, **kw) -> Transaction:
    fields = dict(
        userId=owner.id, payerId=owner.id, date=datetime(2026, 10, 7), amount=120, description="Hosting",
        payMethod="coin", billingCycle="one_time", slackChannel="C-TX", slackTs="111.222",
    )
    return await Transaction(**{**fields, **kw}).insert()


def thread_replies(slack):
    return [b for m, b in slack if m == "chat.postMessage" and b.get("thread_ts") == "111.222"]


# --- endpoint ---------------------------------------------------------------------------------------


async def test_endpoint_401_on_bad_signature_and_503_without_secret(client, slack):
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner)
    body = click_body("tx_approve", str(row.id))
    async with client as c:
        assert (await c.post(PATH, content=body, headers=signed(body, secret="wrong"))).status_code == 401
        stale = str(int(time.time()) - 301)
        assert (await c.post(PATH, content=body, headers=signed(body, ts=stale))).status_code == 401
        assert (await c.post(PATH, content=body + b"x", headers=signed(body))).status_code == 401
        no_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        assert (await c.post(PATH, content=body, headers=no_headers)).status_code == 401
        with patch.object(slack_router, "get_settings", settings(secret="")):
            assert (await c.post(PATH, content=body, headers=signed(body))).status_code == 503
        with patch.object(slack_router, "get_settings", settings(secret=None)):
            assert (await c.post(PATH, content=body, headers=signed(body))).status_code == 503
    assert (await Transaction.get(row.id)).status == "pending"
    assert slack == [] and slack.ephemeral == []


async def test_admin_approves(client, slack):
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    admin = await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner)
    async with client as c:
        r = await click(c, "tx_approve", str(row.id))
    assert r.status_code == 200
    stored = await Transaction.get(row.id)
    assert (stored.status, stored.approvedBy) == ("approved", admin.id) and stored.approvedAt is not None
    assert [m for m, _ in slack] == ["chat.update", "chat.postMessage"]
    approved = ":white_check_mark: <@U0OWNER001> — *Approved* by <@U0ADMIN001> · $120.00"
    assert thread_replies(slack)[0]["text"] == approved
    assert slack.ephemeral == []


async def test_admin_rejects(client, slack):
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    admin = await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner)
    async with client as c:
        r = await click(c, "tx_reject", str(row.id))
    assert r.status_code == 200
    stored = await Transaction.get(row.id)
    assert (stored.status, stored.approvedBy) == ("rejected", admin.id)
    assert thread_replies(slack)[0]["text"] == ":no_entry: <@U0OWNER001> — *Rejected* by <@U0ADMIN001> · $120.00"
    assert slack.ephemeral == []


async def test_non_admin_refused(client, slack):
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    await make_user("Maria", "staff", "U0STAFF001")
    await make_user("Ana", "interview_manager", "U0MANAGER1")
    await make_user("Gone", "admin", "U0GONE0001", active=False)
    row = await make_tx(owner)
    async with client as c:
        for slack_user in ("U0STAFF001", "U0MANAGER1", "U0GONE0001", "U0OWNER001"):
            assert (await click(c, "tx_approve", str(row.id), slack_user)).status_code == 200
    assert (await Transaction.get(row.id)).status == "pending"
    assert slack == []
    assert slack.texts() == ["Only admins can approve or reject transactions in Slack."] * 4
    url, payload = slack.ephemeral[0]
    assert url == RESPONSE_URL
    assert payload == {
        "response_type": "ephemeral", "replace_original": False,
        "text": "Only admins can approve or reject transactions in Slack.",
    }


async def test_unlinked_slack_user_refused(client, slack):
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    await make_user("Root", "admin")  # admin without a Slack ID
    row = await make_tx(owner)
    async with client as c:
        assert (await click(c, "tx_reject", str(row.id), "U0STRANGER")).status_code == 200
    assert (await Transaction.get(row.id)).status == "pending"
    assert slack.texts() == ["Only admins can approve or reject transactions in Slack."]


async def test_second_click_already_decided(client, slack):
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    await make_user("Root", "admin", "U0ADMIN001")
    await make_user("Boss", "admin", "U0ADMIN002")
    row = await make_tx(owner)
    async with client as c:
        assert (await click(c, "tx_approve", str(row.id))).status_code == 200
        assert (await click(c, "tx_reject", str(row.id), "U0ADMIN002")).status_code == 200
        assert (await click(c, "tx_approve", str(row.id))).status_code == 200
    stored = await Transaction.get(row.id)
    assert stored.status == "approved"
    assert len(thread_replies(slack)) == 1
    assert slack.texts() == ["Already approved by Root."] * 2


async def test_simultaneous_clicks_only_one_decides(db, slack):
    """A double-click or two admins at once: exactly one decision lands and one thread reply is posted."""
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    a = await make_user("Root", "admin", "U0ADMIN001")
    b = await make_user("Boss", "admin", "U0ADMIN002")
    row = await make_tx(owner)
    real_get = Transaction.get
    reads = []

    async def get_then_wait(*args, **kw):
        """Both clicks read the row before either writes, as two real requests would interleave."""
        doc = await real_get(*args, **kw)
        reads.append(1)
        while len(reads) < 2:
            await asyncio.sleep(0)
        return doc

    with patch.object(Transaction, "get", get_then_wait):
        results = await asyncio.gather(
            handle_transaction_action("tx_approve", str(row.id), "U0ADMIN001"),
            handle_transaction_action("tx_reject", str(row.id), "U0ADMIN002"),
        )
    assert results.count(None) == 1
    stored = await Transaction.get(row.id)
    winner, loser_text = (a, results[1]) if results[0] is None else (b, results[0])
    assert stored.approvedBy == winner.id
    assert stored.status == ("approved" if winner is a else "rejected")
    assert loser_text == f"Already {stored.status} by {winner.name}."
    assert len(thread_replies(slack)) == 1


async def test_set_status_returns_false_when_the_row_changed_since_read(db, slack):
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    a = await make_user("Root", "admin", "U0ADMIN001")
    b = await make_user("Boss", "admin", "U0ADMIN002")
    row = await make_tx(owner)
    stale = await Transaction.get(row.id)
    assert await st.set_transaction_status(row, "approved", a) is True
    assert await st.set_transaction_status(stale, "rejected", b) is False
    stored = await Transaction.get(row.id)
    assert (stored.status, stored.approvedBy) == ("approved", a.id)
    assert len(thread_replies(slack)) == 1
    # A deliberate change from the current state still works (the app's admin flipping a decision).
    assert await st.set_transaction_status(stored, "rejected", b) is True
    assert (await Transaction.get(row.id)).status == "rejected"


async def test_already_rejected_wording(db, slack):
    owner = await make_user("Pedro")
    admin = await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner, status="rejected", approvedBy=admin.id)
    assert await handle_transaction_action("tx_approve", str(row.id), "U0ADMIN001") == "Already rejected by Root."
    assert (await Transaction.get(row.id)).status == "rejected"


async def test_unknown_transaction(client, slack):
    await make_user("Root", "admin", "U0ADMIN001")
    async with client as c:
        assert (await click(c, "tx_approve", str(PydanticObjectId()))).status_code == 200
        assert (await click(c, "tx_approve", "not-an-object-id")).status_code == 200
        assert (await click(c, "tx_reject", "")).status_code == 200
    assert slack.texts() == ["This transaction no longer exists."] * 3
    assert slack == []


async def test_unrelated_action_ignored(client, slack):
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner)
    async with client as c:
        r1 = await click(c, "something_else", str(row.id))
        other_type = click_body("tx_approve", str(row.id), kind="view_submission")
        r2 = await c.post(PATH, content=other_type, headers=signed(other_type))
        no_payload = urlencode({"foo": "bar"}).encode()
        r3 = await c.post(PATH, content=no_payload, headers=signed(no_payload))
        bad_json = urlencode({"payload": "{not json"}).encode()
        r4 = await c.post(PATH, content=bad_json, headers=signed(bad_json))
    assert [r.status_code for r in (r1, r2, r3, r4)] == [200, 200, 200, 200]
    assert (await Transaction.get(row.id)).status == "pending"
    assert slack == [] and slack.ephemeral == []


async def test_ephemeral_only_goes_to_slack_hooks(db):
    """The reply URL comes from a signed payload, but only Slack's own hooks host is ever called."""
    calls = []

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            calls.append((url, json))
            return SimpleNamespace(status_code=200)

    with patch.object(st.httpx, "AsyncClient", FakeClient):
        await st._post_ephemeral("http://169.254.169.254/latest", {"text": "x"})
        await st._post_ephemeral("https://evil.example/hooks.slack.com/x", {"text": "x"})
        await st._post_ephemeral(RESPONSE_URL, {"text": "ok"})
    assert calls == [(RESPONSE_URL, {"text": "ok"})]
