"""GET /accounts/metrics (in-memory MongoDB via mongomock-motor; routes called directly)."""
import os
from datetime import datetime, timedelta

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)
from app.routers.accounts import list_profile_metrics

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

ROW_KEYS = {
    "accountId", "spanWeeks", "bidsPerWeek", "selfBidsPerWeek", "bidderBidsPerWeek", "interviewsPerWeek",
    "selfInterviewsPerWeek", "callerInterviewsPerWeek", "interviewsPerBid", "lowVolume", "tailorScore", "matchScore",
    "bids", "selfBids", "bidderBids", "interviews", "selfInterviews", "callerInterviews", "tailoredResumes",
    "selfAppliedInterviews", "bidderAppliedInterviews", "unsetAppliedInterviews",
    "selfRate", "bidderRate", "firstApply", "lastApply",
}
OLD = datetime(2020, 1, 1)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["profile-metrics-router-unit"]
    await init_beanie(
        database=database,
        document_models=[
            UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog,
            ResumeApplication,
        ],
    )


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name}@x.io", role=role).insert()


async def _acct(owner, name, **kw):
    return await Account(name=name, createdBy=owner.id, createdAt=OLD, **kw).insert()


async def test_list_shape(db):
    ana = await _user("ana")
    a, b = await _acct(ana, "A"), await _acct(ana, "B")
    await _acct(ana, "Gone", archived=True)
    out = await list_profile_metrics(ana, weeks=4, userId="")
    assert set(out) == {"start", "end", "weeks", "rows", "ratesSince"}
    assert out["weeks"] == 4
    assert datetime.fromisoformat(out["end"]).date() - datetime.fromisoformat(out["start"]).date() == timedelta(days=27)
    assert {r["accountId"] for r in out["rows"]} == {str(a.id), str(b.id)}
    for r in out["rows"]:
        assert set(r) == ROW_KEYS


async def test_list_weeks_validated(db):
    ana = await _user("ana")
    with pytest.raises(HTTPException) as e:
        await list_profile_metrics(ana, weeks=5, userId="")
    assert e.value.status_code == 422


async def test_list_all_time(db):
    ana = await _user("ana")
    a = await _acct(ana, "A")
    out = await list_profile_metrics(ana, weeks="all", userId="")
    assert out["weeks"] == "all" and [r["accountId"] for r in out["rows"]] == [str(a.id)]
    assert out["selfBidsSince"] is None  # tests measure from 0001-01-01: no launch day to name
    assert set(out["rows"][0]) == ROW_KEYS
    with pytest.raises(HTTPException) as e:
        await list_profile_metrics(ana, weeks="forever", userId="")
    assert e.value.status_code == 422


async def test_list_other_user_forbidden(db):
    ana, ben = await _user("ana"), await _user("ben")
    with pytest.raises(HTTPException) as e:
        await list_profile_metrics(ana, weeks=4, userId=str(ben.id))
    assert e.value.status_code == 403


async def test_list_admin_user_filter(db):
    ana, ben, boss = await _user("ana"), await _user("ben"), await _user("boss", "admin")
    a = await _acct(ana, "A")
    await _acct(ben, "B")
    out = await list_profile_metrics(boss, weeks=4, userId=str(ana.id))
    assert [r["accountId"] for r in out["rows"]] == [str(a.id)]
    assert len((await list_profile_metrics(boss, weeks=4, userId=""))["rows"]) == 2
