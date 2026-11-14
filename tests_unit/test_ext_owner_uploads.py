"""Owners' applies from the extension become self bids (spec 2026-11-15-self-bids-extension-design.md): one bid per
job, kind="self", the owner and the signed-in profile, approved, no bidder; kept out of bidder review, the pay week
board and bidder performance (mongomock-motor, ASGI client)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.security import create_ext_owner_token, hash_password

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


class _Storage:
    def presign_put(self, bucket, key, content_type="image/png"):
        return f"https://upload.example/{key}"


@pytest.fixture
async def api(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.routers import owner_ext

    monkeypatch.setenv("BID_STORAGE_BUCKET", "bid-screenshots")
    from app.config import get_settings
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    monkeypatch.setattr(owner_ext, "get_bid_storage", lambda: _Storage())
    await init_beanie(database=AsyncMongoMockClient()["ext-owner-uploads-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, Interview, InterviewQuestion])
    app = FastAPI()
    app.include_router(owner_ext.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None


async def setup():
    pedro = await UserExtra(name="Pedro", email="pedro@x.io", hashedPassword=hash_password("x")).insert()
    alpha = await Account(name="Alpha", createdBy=pedro.id).insert()
    return pedro, alpha, {"Authorization": f"Bearer {create_ext_owner_token(str(pedro.id), str(alpha.id))}"}


def shot(step=1, trigger="step", job="Acme - Backend Engineer"):
    return {"job": job, "step": step, "trigger": trigger, "capturedAt": datetime.now(timezone.utc).isoformat(),
            "jobUrl": "https://jobs.example/acme/123", "jobTitle": "Backend Engineer", "format": "webp"}


async def test_an_apply_becomes_one_approved_self_bid(api):
    pedro, alpha, auth = await setup()
    r = await api.post("/ext/owner/upload-url", json=shot(1), headers=auth)
    assert r.status_code == 200
    assert r.json()["key"].startswith(f"self-{pedro.id}/") and "/alpha/" in r.json()["key"].lower()
    await api.post("/ext/owner/upload-url", json=shot(2, "submit"), headers=auth)    # same job, second screenshot
    bids = await Bid.find_all().to_list()
    assert len(bids) == 1
    b = bids[0]
    assert (b.kind, b.bidderId, b.ownerId, b.accountId, b.status) == ("self", None, pedro.id, alpha.id, "approved")
    assert len(b.screenshots) == 2 and b.submittedAt is not None and b.jobUrl == "https://jobs.example/acme/123"


async def test_confirm_marks_the_screenshot_uploaded(api):
    _pedro, _alpha, auth = await setup()
    key = (await api.post("/ext/owner/upload-url", json=shot(), headers=auth)).json()["key"]
    assert (await api.post("/ext/owner/uploads/confirm", json={"key": key}, headers=auth)).status_code == 200
    assert (await Bid.find_one()).screenshots[0].uploadedAt is not None
    assert (await api.post("/ext/owner/uploads/confirm", json={"key": "nope/x"}, headers=auth)).status_code == 404


async def test_self_bids_stay_out_of_bidder_numbers(api, monkeypatch):
    from app.bids.performance import performance
    from app.bids.week import pay_week, week_board
    from app.bids.windows import schedule_from_settings
    from app.routers import bids as bids_router

    pedro, _alpha, auth = await setup()
    ana = await Bidder(createdBy=pedro.id, name="Ana", rate=0.5, folder="ana", screenshotFolderUrl="s3://x/",
                       registeredAt=datetime(2026, 1, 1), createdAt=datetime(2026, 1, 1)).insert()
    await api.post("/ext/owner/upload-url", json=shot(), headers=auth)
    sched, now = schedule_from_settings(), datetime.now(timezone.utc)
    board = await week_board([ana], pay_week(sched, now), now, sched, _Storage())
    assert board["totals"]["approved"] == 0
    today = now.astimezone(sched.tz).date()
    perf = await performance([ana], "week", today, today, "", today)
    assert perf["totals"]["total"] == 0
    # The pending-review list and bulk review only ever see bidders' bids.
    from app.bids.records import count_by_status
    assert sum(sum(v) for v in (await count_by_status([ana.id], now.replace(year=now.year - 1), now)).values()) == 0
    bid = await Bid.find_one()
    allowed = bids_router._can_review(None, pedro)
    assert allowed is False and bid.bidderId is None


async def test_a_bidder_token_cannot_upload_as_an_owner(api):
    pedro, _alpha, _auth = await setup()
    from app.security import create_bidder_token
    b = await Bidder(createdBy=pedro.id, name="Ana", rate=0.5, folder="ana", screenshotFolderUrl="s3://x/",
                     registeredAt=datetime(2026, 1, 1)).insert()
    r = await api.post("/ext/owner/upload-url", json=shot(),
                       headers={"Authorization": f"Bearer {create_bidder_token(str(b.id), b.tokenVersion)}"})
    assert r.status_code == 401
