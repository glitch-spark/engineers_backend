"""Transaction posts in Slack: text and blocks (pure), then post / update / repost / decision flows
and the router hooks, with Slack faked and an in-memory MongoDB (mongomock-motor). No network, no real DB."""

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app import slack_transactions as st  # noqa: E402
from app.models import Transaction, UserExtra  # noqa: E402
from app.models.recurring_charge import RecurringCharge  # noqa: E402
from app.slack_transactions import (  # noqa: E402
    announce_decision,
    delete_transaction_post,
    fmt_usd,
    set_transaction_status,
    sync_transaction_post,
    transaction_blocks,
    transaction_text,
)

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")

TX_ID = PydanticObjectId()


def user(name, slack=None):
    return SimpleNamespace(name=name, email=f"{name.lower()}@x.io", slackUserId=slack)


OWNER, PAYER, ADMIN = user("Pedro", "U0OWNER001"), user("Maria", "U0PAYER001"), user("Root", "U0ADMIN001")


def tx(**kw):
    base = dict(
        id=TX_ID, amount=120, date=datetime(2026, 10, 7), status="pending", payMethod="card",
        cardLast4="1234", cardLabel=None, billingCycle="monthly", description="Hosting", notes="",
        slackChannel=None, slackTs=None,
    )
    return SimpleNamespace(**{**base, **kw})


def settings(channel="C-TX", secret="shh"):
    return lambda: SimpleNamespace(SLACK_TRANSACTIONS_CHANNEL_ID=channel, SLACK_SIGNING_SECRET=secret)


# --- text -------------------------------------------------------------------------------------------


def test_fmt_usd():
    assert fmt_usd(1234.5) == "$1,234.50"
    assert fmt_usd(0) == "$0.00"


def test_text_card_monthly():
    text = transaction_text(
        tx(amount=120, payMethod="card", cardLast4="1234", cardLabel="Ops card", billingCycle="monthly",
           description="Hosting"),
        OWNER, PAYER,
    )
    assert text.splitlines() == [
        ":money_with_wings: <@U0OWNER001> — *New transaction* · $120.00",
        "*Payer:* <@U0PAYER001>", "*Date:* Oct 7", "*Method:* Card ••1234 (Ops card)", "*Billing:* Monthly",
        "*Description:* Hosting"]


def test_text_coin_one_time_no_description_with_notes():
    lines = transaction_text(
        tx(payMethod="coin", cardLast4=None, billingCycle="one_time", description="", notes="n1"), OWNER, OWNER
    ).splitlines()
    assert "*Method:* Coin" in lines and "*Billing:* One-time" in lines and "*Notes:* n1" in lines
    assert not any(line.startswith("*Description:*") for line in lines)


def test_text_card_without_label_and_legacy_crypto():
    assert "*Method:* Card ••1234" in transaction_text(tx(cardLabel=""), OWNER, OWNER).splitlines()
    assert "*Method:* Coin" in transaction_text(tx(payMethod="crypto", cardLast4=None), OWNER, OWNER).splitlines()


def test_text_leaves_out_lines_without_a_value():
    lines = transaction_text(
        tx(payMethod=None, cardLast4=None, billingCycle=None, description=None, notes=None), OWNER, OWNER
    ).splitlines()
    assert [line.split(":*")[0] for line in lines[1:]] == ["*Payer", "*Date"]


def test_text_escapes_user_text():
    text = transaction_text(tx(description="<!channel> pay me", notes="a & b", cardLabel="<!here>"), OWNER, OWNER)
    assert "&lt;!channel&gt; pay me" in text and "a &amp; b" in text and "(&lt;!here&gt;)" in text
    assert "<!channel>" not in text and "<!here>" not in text


def test_names_without_slack_id():
    assert transaction_text(tx(), user("Pedro"), user("Maria")).splitlines()[:2] == [
        ":money_with_wings: Pedro — *New transaction* · $120.00", "*Payer:* Maria"]


def test_long_description_is_cut_to_fit_a_section():
    with patch.object(st, "get_settings", settings()):
        text = transaction_blocks(tx(description="x" * 5000), OWNER, OWNER, None)[0]["text"]["text"]
    assert len(text) == 3000 and text.endswith("…")


def test_name_that_looks_like_a_ping_is_escaped():
    assert transaction_text(tx(), user("<!channel>"), OWNER).splitlines()[0].startswith(
        ":money_with_wings: &lt;!channel&gt; — ")


# --- blocks -----------------------------------------------------------------------------------------


def test_buttons_only_when_pending_and_secret():
    with patch.object(st, "get_settings", settings()):
        blocks = transaction_blocks(tx(), OWNER, PAYER, None)
    assert [b["type"] for b in blocks] == ["section", "actions"]
    assert blocks[0]["text"] == {"type": "mrkdwn", "text": transaction_text(tx(), OWNER, PAYER)}
    approve, reject = blocks[1]["elements"]
    assert (approve["action_id"], approve["value"], approve["style"]) == ("tx_approve", str(TX_ID), "primary")
    assert (reject["action_id"], reject["value"], reject["style"]) == ("tx_reject", str(TX_ID), "danger")
    assert (approve["text"]["text"], reject["text"]["text"]) == ("Approve", "Reject")

    with patch.object(st, "get_settings", settings(secret="")):
        assert [b["type"] for b in transaction_blocks(tx(), OWNER, PAYER, None)] == ["section"]


def test_decided_post_replaces_buttons_with_the_decision():
    with patch.object(st, "get_settings", settings()):
        approved = transaction_blocks(tx(status="approved"), OWNER, PAYER, ADMIN)
        rejected = transaction_blocks(tx(status="rejected"), OWNER, PAYER, ADMIN)
    assert [b["type"] for b in approved] == ["section", "section"]
    assert approved[1]["text"] == {"type": "mrkdwn", "text": ":white_check_mark: *Approved* by <@U0ADMIN001>"}
    assert rejected[1]["text"] == {"type": "mrkdwn", "text": ":no_entry: *Rejected* by <@U0ADMIN001>"}


# --- fixtures ---------------------------------------------------------------------------------------


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["slack-tx-unit"]
    await init_beanie(database=database, document_models=[Transaction, UserExtra, RecurringCharge])
    return database


class FakeSlack(list):
    """The recorded (method, body) calls. `fail[method] = error` makes that method's next call raise."""

    def __init__(self):
        super().__init__()
        self.fail: dict[str, str] = {}
        self.posts = 0

    async def api(self, method, body):
        self.append((method, body))
        if method in self.fail:
            raise RuntimeError(self.fail.pop(method))
        if method != "chat.postMessage":
            return {"ok": True}
        self.posts += 1
        return {"ok": True, "ts": f"900.{self.posts:03d}", "channel": body.get("channel")}


@pytest.fixture
def slack():
    """Fake the Slack API and the bot's settings."""
    fake = FakeSlack()
    with (
        patch.object(st, "_slack_api", fake.api),
        patch.object(st, "slack_bot_configured", lambda: True),
        patch.object(st, "get_settings", settings()),
    ):
        yield fake


async def make_user(name, role="staff", slack_id=None) -> UserExtra:
    return await UserExtra(
        name=name, email=f"{name.lower()}@x.io", role=role, isActive=True, slackUserId=slack_id
    ).insert()


async def make_tx(owner, payer=None, **kw) -> Transaction:
    fields = dict(
        userId=owner.id, payerId=(payer or owner).id, date=datetime(2026, 10, 7), amount=120,
        description="Hosting", payMethod="coin", billingCycle="one_time",
    )
    return await Transaction(**{**fields, **kw}).insert()


# --- sync_transaction_post --------------------------------------------------------------------------


async def test_sync_posts_then_updates_then_reposts_gone(db, slack):
    owner, payer = await make_user("Pedro", slack_id="U0OWNER001"), await make_user("Maria", slack_id="U0PAYER001")
    row = await make_tx(owner, payer)

    await sync_transaction_post(row)
    method, body = slack[0]
    assert method == "chat.postMessage" and body["channel"] == "C-TX" and "thread_ts" not in body
    assert body["text"].startswith(":money_with_wings: <@U0OWNER001> — *New transaction* · $120.00")
    assert [b["type"] for b in body["blocks"]] == ["section", "actions"]
    stored = await Transaction.get(row.id)
    assert (stored.slackChannel, stored.slackTs) == ("C-TX", "900.001")

    row.amount = 99
    await sync_transaction_post(row)
    method, body = slack[1]
    assert method == "chat.update" and (body["channel"], body["ts"]) == ("C-TX", "900.001")
    assert "$99.00" in body["text"]

    slack.fail["chat.update"] = "message_not_found"
    await sync_transaction_post(row)
    assert [c[0] for c in slack[2:]] == ["chat.update", "chat.postMessage"]
    stored = await Transaction.get(row.id)
    assert (stored.slackChannel, stored.slackTs) == ("C-TX", "900.002")


async def test_sync_keeps_decided_state_on_a_decided_transaction(db, slack):
    owner, admin = await make_user("Pedro", slack_id="U0OWNER001"), await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner, status="approved", approvedBy=admin.id)
    await sync_transaction_post(row)
    blocks = slack[0][1]["blocks"]
    assert [b["type"] for b in blocks] == ["section", "section"]
    assert blocks[1]["text"]["text"] == ":white_check_mark: *Approved* by <@U0ADMIN001>"


async def test_sync_never_raises_and_stores_nothing_on_failure(db, slack):
    row = await make_tx(await make_user("Pedro"))
    slack.fail["chat.postMessage"] = "invalid_auth"
    await sync_transaction_post(row)
    stored = await Transaction.get(row.id)
    assert (stored.slackChannel, stored.slackTs) == (None, None)


async def test_no_channel_configured_is_noop(db, slack):
    row = await make_tx(await make_user("Pedro"))
    with patch.object(st, "get_settings", settings(channel="")):
        await sync_transaction_post(row)
        await announce_decision(row, await make_user("Root", "admin"))
    assert slack == []


async def test_bot_not_configured_is_noop(db, slack):
    row = await make_tx(await make_user("Pedro"))
    with patch.object(st, "slack_bot_configured", lambda: False):
        await sync_transaction_post(row)
        await announce_decision(row, await make_user("Root", "admin"))
        await delete_transaction_post(row)
    assert slack == []


# --- decisions --------------------------------------------------------------------------------------


async def test_set_status_updates_post_and_replies_in_thread(db, slack):
    owner, admin = await make_user("Pedro", slack_id="U0OWNER001"), await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner, slackChannel="C-TX", slackTs="111.222")

    await set_transaction_status(row, "approved", admin)

    stored = await Transaction.get(row.id)
    assert (stored.status, stored.approvedBy) == ("approved", admin.id) and stored.approvedAt is not None
    (m1, b1), (m2, b2) = slack
    assert m1 == "chat.update" and (b1["channel"], b1["ts"]) == ("C-TX", "111.222")
    assert [b["type"] for b in b1["blocks"]] == ["section", "section"]
    assert b1["blocks"][1]["text"]["text"] == ":white_check_mark: *Approved* by <@U0ADMIN001>"
    assert m2 == "chat.postMessage" and (b2["channel"], b2["thread_ts"]) == ("C-TX", "111.222")
    assert b2["text"] == ":white_check_mark: <@U0OWNER001> — *Approved* by <@U0ADMIN001> · $120.00"


async def test_rejection_wording(db, slack):
    owner, admin = await make_user("Pedro", slack_id="U0OWNER001"), await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner, slackChannel="C-TX", slackTs="111.222")
    await set_transaction_status(row, "rejected", admin)
    assert slack[1][1]["text"] == ":no_entry: <@U0OWNER001> — *Rejected* by <@U0ADMIN001> · $120.00"
    assert slack[0][1]["blocks"][1]["text"]["text"] == ":no_entry: *Rejected* by <@U0ADMIN001>"


async def test_decision_without_post_is_standalone(db, slack):
    owner, admin = await make_user("Pedro", slack_id="U0OWNER001"), await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner)
    await set_transaction_status(row, "approved", admin)
    assert [c[0] for c in slack] == ["chat.postMessage"]
    body = slack[0][1]
    assert body["channel"] == "C-TX" and "thread_ts" not in body
    assert body["text"] == ":white_check_mark: <@U0OWNER001> — *Approved* by <@U0ADMIN001> · $120.00"


async def test_decision_on_a_gone_post_is_standalone(db, slack):
    owner, admin = await make_user("Pedro", slack_id="U0OWNER001"), await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner, slackChannel="C-TX", slackTs="111.222")
    slack.fail["chat.update"] = "message_not_found"
    await set_transaction_status(row, "approved", admin)
    assert [c[0] for c in slack] == ["chat.update", "chat.postMessage"]
    assert "thread_ts" not in slack[1][1]


async def test_set_status_survives_slack_failure(db, slack):
    owner, admin = await make_user("Pedro"), await make_user("Root", "admin")
    row = await make_tx(owner, slackChannel="C-TX", slackTs="111.222")
    slack.fail["chat.update"] = "ratelimited"
    await set_transaction_status(row, "approved", admin)
    assert (await Transaction.get(row.id)).status == "approved"


async def test_delete_post(db, slack):
    row = await make_tx(await make_user("Pedro"), slackChannel="C-TX", slackTs="111.222")
    await delete_transaction_post(row)
    assert slack == [("chat.delete", {"channel": "C-TX", "ts": "111.222"})]

    slack.clear()
    await delete_transaction_post(await make_tx(await make_user("Ana")))  # never posted
    assert slack == []

    slack.fail["chat.delete"] = "message_not_found"
    await delete_transaction_post(row)  # never raises


# --- router hooks -----------------------------------------------------------------------------------


@pytest.fixture
def client(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import transactions

    def make(current):
        app = FastAPI()
        app.include_router(transactions.router)
        app.dependency_overrides[get_current_user] = lambda: current
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


async def test_create_posts_to_the_channel(client, slack):
    pedro = await make_user("Pedro", slack_id="U0OWNER001")
    async with client(pedro) as c:
        r = await c.post("/transactions", json={
            "date": "2026-10-07", "amount": 120, "payMethod": "coin", "billingCycle": "one_time",
            "description": "Hosting"})
    assert r.status_code == 201, r.text
    assert [c[0] for c in slack] == ["chat.postMessage"]
    assert slack[0][1]["text"].startswith(":money_with_wings: <@U0OWNER001> — *New transaction* · $120.00")
    stored = await Transaction.get(PydanticObjectId(r.json()["_id"]))
    assert (stored.slackChannel, stored.slackTs) == ("C-TX", "900.001")


async def test_create_still_succeeds_when_slack_fails(client, slack):
    slack.fail["chat.postMessage"] = "invalid_auth"
    async with client(await make_user("Pedro")) as c:
        r = await c.post("/transactions", json={"date": "2026-10-07", "amount": 5, "payMethod": "coin"})
    assert r.status_code == 201


async def test_admin_approves_through_the_route(client, slack):
    owner, admin = await make_user("Pedro", slack_id="U0OWNER001"), await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner, slackChannel="C-TX", slackTs="111.222")
    async with client(admin) as c:
        r = await c.put(f"/transactions/{row.id}", json={"status": "approved"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert [c[0] for c in slack] == ["chat.update", "chat.postMessage"]
    assert slack[1][1]["text"] == ":white_check_mark: <@U0OWNER001> — *Approved* by <@U0ADMIN001> · $120.00"
    stored = await Transaction.get(row.id)
    assert (stored.status, stored.approvedBy) == ("approved", admin.id)


async def test_payer_may_reject_through_the_route(client, slack):
    owner, payer = await make_user("Pedro"), await make_user("Maria", slack_id="U0PAYER001")
    row = await make_tx(owner, payer, slackChannel="C-TX", slackTs="111.222")
    async with client(payer) as c:
        r = await c.put(f"/transactions/{row.id}", json={"status": "rejected"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert slack[1][1]["text"].startswith(":no_entry: Pedro — *Rejected* by <@U0PAYER001>")


async def test_owner_who_is_not_payer_cannot_approve(client, slack):
    owner, payer = await make_user("Pedro"), await make_user("Maria")
    row = await make_tx(owner, payer)
    async with client(owner) as c:
        r = await c.put(f"/transactions/{row.id}", json={"status": "approved"})
    assert r.status_code == 403
    assert (await Transaction.get(row.id)).status == "pending"
    assert slack == []


async def test_unchanged_status_posts_nothing(client, slack):
    owner, admin = await make_user("Pedro"), await make_user("Root", "admin")
    row = await make_tx(owner, status="approved", approvedBy=admin.id)
    async with client(admin) as c:
        r = await c.put(f"/transactions/{row.id}", json={"status": "approved"})
    assert r.status_code == 200
    assert slack == []


async def test_editing_a_pending_transaction_updates_the_post(client, slack):
    owner = await make_user("Pedro", slack_id="U0OWNER001")
    row = await make_tx(owner, slackChannel="C-TX", slackTs="111.222")
    async with client(owner) as c:
        r = await c.put(f"/transactions/{row.id}", json={"amount": 99, "description": "Domains"})
    assert r.status_code == 200
    assert [c[0] for c in slack] == ["chat.update"]
    body = slack[0][1]
    assert (body["channel"], body["ts"]) == ("C-TX", "111.222")
    assert "$99.00" in body["text"] and "*Description:* Domains" in body["text"]


async def test_approve_with_edits_announces_the_edited_amount(client, slack):
    owner, admin = await make_user("Pedro", slack_id="U0OWNER001"), await make_user("Root", "admin", "U0ADMIN001")
    row = await make_tx(owner, slackChannel="C-TX", slackTs="111.222")
    async with client(admin) as c:
        r = await c.put(f"/transactions/{row.id}", json={"status": "approved", "amount": 75})
    assert r.status_code == 200
    assert (await Transaction.get(row.id)).amount == 75
    assert slack[1][1]["text"].endswith("· $75.00")


async def test_invalid_edit_with_a_status_change_changes_nothing(client, slack):
    owner, admin = await make_user("Pedro"), await make_user("Root", "admin")
    row = await make_tx(owner)
    async with client(admin) as c:
        r = await c.put(f"/transactions/{row.id}", json={"status": "approved", "payMethod": "card", "cardLast4": "12"})
    assert r.status_code == 400
    assert (await Transaction.get(row.id)).status == "pending"
    assert slack == []


async def test_delete_removes_the_post(client, slack):
    owner = await make_user("Pedro")
    row = await make_tx(owner, slackChannel="C-TX", slackTs="111.222")
    async with client(owner) as c:
        r = await c.delete(f"/transactions/{row.id}")
    assert r.status_code == 200
    assert slack == [("chat.delete", {"channel": "C-TX", "ts": "111.222"})]
    assert await Transaction.get(row.id) is None
