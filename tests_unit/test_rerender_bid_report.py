"""scripts/rerender_bid_report: recount one stored bid report under the current schedule and edit its Slack post in
place (mongomock; Slack patched, never reached)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bid_report import BidReport
from app.models.bid_report_post import BidReportPost
from app.models.bidder import Bidder
from scripts import rerender_bid_report as rr

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 16, 23, 0, tzinfo=CHI).astimezone(timezone.utc)  # Mon 23:00, after Monday's report day
OLD = (datetime(2026, 11, 16, 0, 30), datetime(2026, 11, 17, 0, 30))  # Sun 18:30 -> Mon 18:30 CST (old rules), naive UTC
NEW = (datetime(2026, 11, 16, 0, 30, tzinfo=timezone.utc), datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc))


def utc(local: datetime) -> datetime:
    return local.replace(tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
async def db(monkeypatch):
    from app.bids import records, reports, windows
    from app.config import get_settings

    database = AsyncMongoMockClient()["rerender-bid-report-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Account, Bidder, Bid, BidReport, BidReportPost])
    settings = get_settings().model_copy(update={
        "BID_RECORDS_START": None, "BID_SCHEDULE_SWITCH_AT": "2026-11-15T20:00", "BID_NEXT_DAILY_REPORT_TIME": "18:00",
        "BID_NEXT_WEEKLY_REPORT_DAY": "sunday", "BID_NEXT_WEEKLY_REPORT_TIME": "20:00", "BID_SCHEDULE_SHORT_WEEK": True,
        "SLACK_BOT_TOKEN": "xoxb-test", "SLACK_BIDS_CHANNEL_ID": "CBIDS"})
    for module in (records, windows, reports):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(rr, "get_settings", lambda: settings)
    return database


@pytest.fixture
def slack(monkeypatch):
    """Every Slack call is recorded here; nothing leaves the process."""
    from app.bids import reports

    calls = []

    async def fake(method, body):
        calls.append((method, body))
        return {"ok": True, "ts": "999.0", "channel": body.get("channel")}
    monkeypatch.setattr(reports, "_slack_api", fake)
    return calls


async def seed(post=True):
    """Ana's daily 2026-11-16 was stored under the old 18:30 cutoff: 3 approved (one at 18:10, after the new 18:00
    cutoff), 1 pending. Under the current schedule the day ends at 18:00: 2 approved, 1 pending."""
    pedro = await UserExtra(name="Pedro", email="pedro@x.io", role="staff").insert()
    ana = await Bidder(createdBy=pedro.id, name="Ana", rate=0.5, folder="ana", screenshotFolderUrl="s3://shots/ana/",
                       createdAt=datetime(2026, 1, 1), registeredAt=datetime(2026, 1, 1)).insert()
    for i, (status, at) in enumerate([("approved", datetime(2026, 11, 16, 9)), ("approved", datetime(2026, 11, 16, 12)),
                                      ("pending", datetime(2026, 11, 16, 13)), ("approved", datetime(2026, 11, 16, 18, 10))]):
        await Bid(bidderId=ana.id, ownerId=pedro.id, jobKey=f"ana/x/j{i}", jobUrl="https://j.example/x", jobTitle="x",
                  firstAt=utc(at), status=status, screenshots=[]).insert()
    await BidReport(kind="daily", periodKey="2026-11-16", periodStart=OLD[0], periodEnd=OLD[1], bidderId=ana.id,
                    ownerId=pedro.id, bidderName="Ana", rate=0.5, count=3, amount=1.5, pending=1, rejected=0).insert()
    if post:
        await BidReportPost(kind="daily", periodKey="2026-11-16", channel="CBIDS", ts=["111.0"], dirty=True).insert()
    return ana


async def run(capsys, apply, **kw):
    code = await rr.rerender("daily", "2026-11-16", apply=apply, now=NOW, **kw)
    return code, capsys.readouterr().out


async def test_dry_run_prints_both_windows_and_counts_and_writes_nothing(db, slack, capsys):
    await seed()
    code, out = await run(capsys, apply=False)
    assert code == 0
    assert "Old window: 2026-11-15 18:30 -> 2026-11-16 18:30" in out
    assert "New window: 2026-11-15 18:30 -> 2026-11-16 18:00" in out
    line = next(l for l in out.splitlines() if l.strip().startswith("Ana"))
    assert line.split()[1:] == ["3/1/0", "->", "2/1/0", "*"]
    assert "DRY RUN" in out
    (row,) = await BidReport.find_all().to_list()
    assert (row.count, row.periodEnd) == (3, OLD[1])
    assert slack == []


async def test_apply_replaces_rows_and_edits_the_post_in_place(db, slack, capsys):
    ana = await seed()
    code, out = await run(capsys, apply=True)
    assert code == 0
    (row,) = await BidReport.find(BidReport.kind == "daily", BidReport.periodKey == "2026-11-16").to_list()
    assert (row.bidderId, row.count, row.pending, row.rejected, row.amount) == (ana.id, 2, 1, 0, 1.0)
    assert (row.periodStart.replace(tzinfo=timezone.utc), row.periodEnd.replace(tzinfo=timezone.utc)) == NEW
    assert [m for m, _ in slack] == ["chat.update"]  # never chat.postMessage
    body = slack[0][1]
    assert (body["channel"], body["ts"]) == ("CBIDS", "111.0")
    assert "Ana" in str(body["blocks"])
    post = await BidReportPost.find_one(BidReportPost.periodKey == "2026-11-16")
    assert (post.ts, post.dirty) == (["111.0"], False)

    # Twice is safe: same rows, the same post edited again, nothing new posted.
    code, out = await run(capsys, apply=True)
    assert code == 0
    assert await BidReport.find(BidReport.kind == "daily", BidReport.periodKey == "2026-11-16").count() == 1
    assert [m for m, _ in slack] == ["chat.update", "chat.update"]
    assert await BidReportPost.count() == 1


async def test_without_a_stored_post_nothing_is_posted(db, slack, capsys):
    await seed(post=False)
    code, out = await run(capsys, apply=True)
    assert "no stored Slack post" in out
    assert slack == []
    assert await BidReportPost.count() == 0


async def test_a_period_still_open_is_refused(db, slack, capsys):
    await seed()
    code = await rr.rerender("daily", "2026-11-17", apply=True, now=NOW)
    out = capsys.readouterr().out
    assert code == 1 and "not closed" in out
    assert slack == [] and await BidReport.find(BidReport.periodKey == "2026-11-17").count() == 0


async def test_my_bids_ride_along_in_the_edit(db, slack, capsys):
    ana = await seed()
    alpha = await Account(name="Alpha", createdBy=ana.createdBy).insert()
    await Bid(kind="self", ownerId=ana.createdBy, accountId=alpha.id, jobKey="self-1",
              firstAt=utc(datetime(2026, 11, 16, 10))).insert()
    code, _ = await run(capsys, apply=True)
    assert code == 0 and "My bids" in str(slack[0][1]["blocks"])
