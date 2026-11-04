"""Profile metrics: the completed Monday-Sunday weeks measured, the Bid Track day a bid belongs to, and (in-memory
MongoDB via mongomock-motor) the loaders for self and bidder bids, interview rounds and resume scores per profile and
week, and the row built from them."""
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import profile_metrics as pm
from app.bids.windows import BidSchedule, schedule_from_settings
from app.config import Settings, get_settings
from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)
from app.profile_metrics import bid_report_day, span

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

TODAY = date(2026, 11, 4)
LONG_AGO = date(2025, 1, 1)
SCHED = BidSchedule(tz=ZoneInfo("America/Chicago"), daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))


def test_span_full_window():
    assert span(4, TODAY, date(2026, 1, 1), LONG_AGO) == (date(2026, 10, 5), date(2026, 11, 1), 4)


def test_span_since_midweek():
    assert span(4, TODAY, date(2026, 10, 21), LONG_AGO) == (date(2026, 10, 26), date(2026, 11, 1), 1)


def test_span_since_monday():
    assert span(4, TODAY, date(2026, 10, 19), LONG_AGO) == (date(2026, 10, 19), date(2026, 11, 1), 2)


def test_span_created_midweek():
    assert span(4, TODAY, date(2026, 1, 1), date(2026, 10, 28)) == (date(2026, 10, 26), date(2026, 11, 1), 1)


def test_span_no_completed_week():
    assert span(4, TODAY, date(2026, 11, 3), LONG_AGO)[2] == 0


def test_since_defaults_to_the_release_monday(monkeypatch):
    monkeypatch.delenv("PROFILE_METRICS_SINCE", raising=False)
    assert Settings(_env_file=None).PROFILE_METRICS_SINCE == "2026-11-10"
    assert Settings(_env_file=None, PROFILE_METRICS_SINCE="").PROFILE_METRICS_SINCE is None


def test_since_date(monkeypatch):
    assert pm.since_date() == date.min  # tests_unit/conftest.py blanks it
    dated = get_settings().model_copy(update={"PROFILE_METRICS_SINCE": "2026-11-10"})
    monkeypatch.setattr(pm, "get_settings", lambda: dated)
    assert pm.since_date() == date(2026, 11, 10)


def test_bid_report_day_cutoff():
    assert bid_report_day(datetime(2026, 10, 20, 23, 29), SCHED) == date(2026, 10, 20)
    assert bid_report_day(datetime(2026, 10, 20, 23, 30), SCHED) == date(2026, 10, 21)


MON = date(2026, 10, 19)  # Monday of the week Oct 19-25
START, END = date(2026, 10, 12), date(2026, 10, 25)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["profile-metrics-unit"]
    await init_beanie(
        database=database,
        document_models=[
            UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog,
            ResumeApplication,
        ],
    )


async def _setup():
    user = await UserExtra(name="Ana", email="ana@x.io").insert()
    acc = await Account(name="Ana US", createdBy=user.id, createdAt=datetime(2026, 1, 1)).insert()
    return user, acc


async def _plan(user, day: date, by_profile: dict, logged=True):
    await DailyPlan(
        userId=user.id, date=datetime(day.year, day.month, day.day), year=day.year, weekNumber=43,
        done={"bidsSelfByProfile": by_profile},
        loggedAt=datetime(day.year, day.month, day.day, 20) if logged else None,
    ).insert()


async def test_self_bids_logged_days_only(db):
    user, acc = await _setup()
    key = str(acc.id)
    await _plan(user, date(2026, 10, 20), {key: 5})
    await _plan(user, date(2026, 10, 21), {key: 7}, logged=False)
    assert await pm.self_bids([user.id], {key}, START, END) == {(key, MON): 5}


async def test_self_bids_ignores_unknown_keys(db):
    user, acc = await _setup()
    await _plan(user, date(2026, 10, 20), {str(acc.id): 5, "f" * 24: 9})
    assert await pm.self_bids([user.id], {str(acc.id)}, START, END) == {(str(acc.id), MON): 5}


async def test_bidder_bids_statuses(db):
    user, acc = await _setup()
    live = await Bidder(createdBy=user.id, name="B1", profileId=acc.id, rate=1, screenshotFolderUrl="x").insert()
    old = await Bidder(
        createdBy=user.id, name="B2", profileId=acc.id, rate=1, screenshotFolderUrl="y",
        archivedAt=datetime(2026, 10, 1),
    ).insert()
    other = await Bidder(createdBy=user.id, name="B3", rate=1, screenshotFolderUrl="z").insert()
    at = datetime(2026, 10, 21, 15)
    bids = [(live, "approved"), (live, "pending"), (live, "rejected"), (old, "approved"), (other, "approved")]
    for i, (bidder, status) in enumerate(bids):
        await Bid(bidderId=bidder.id, ownerId=user.id, jobKey=f"k{i}", firstAt=at, status=status).insert()
    got = await pm.bidder_bids([acc.id], START, END, schedule_from_settings())
    assert got == {(str(acc.id), MON): 3}


async def _round(user, acc, *rounds):
    """rounds: (stage, when, status, caller enabled)."""
    history = [
        {"stage": s, "at": w, "scheduledAt": w, "status": st, "caller": {"enabled": True} if c else None}
        for s, w, st, c in rounds
    ]
    await Interview(
        accountId=acc.id, createdBy=user.id, scheduledAt=rounds[-1][1], companyName="Acme", stageHistory=history,
    ).insert()


async def test_interview_rounds_self_caller(db):
    user, acc = await _setup()
    await _round(
        user, acc,
        ("intro", datetime(2026, 10, 20, 15), "completed", False),
        ("tech", datetime(2026, 10, 21, 15), "completed", True),
        ("hiring_manager", datetime(2026, 10, 22, 15), "canceled", False),
    )
    assert await pm.interview_rounds([acc.id], START, END) == {(str(acc.id), MON): [1, 1]}


async def test_interview_week_uses_local_day(db, monkeypatch):
    monkeypatch.setattr(pm, "app_tz", lambda: ZoneInfo("America/New_York"))
    user, acc = await _setup()
    await _round(user, acc, ("intro", datetime(2026, 10, 26, 3, 0), "completed", False))  # Sun Oct 25 23:00 local
    assert await pm.interview_rounds([acc.id], START, END) == {(str(acc.id), MON): [1, 0]}


async def test_scores_split(db):
    user, acc = await _setup()
    key = str(acc.id)
    at = datetime(2026, 10, 21, 12)

    async def application(**kw):
        return await ResumeApplication(
            userId=user.id, accountId=acc.id, companyName="A", companyNameLower="a", jobDescription="jd", **kw,
        ).insert()

    tailored = await application(matchScore={"total": 80, "band": "strong"})
    fixed = await application(matchScore={"total": 60, "band": "ok"}, fixedFrom="r1")

    async def log(i, **kw):
        await JobApplicationLog(userId=user.id, url=f"u{i}", accountId=key, fileKey=f"f{i}", appliedAt=at, **kw).insert()

    await log(1, resumeId="r1", score={"total": 70, "band": "ok"})
    await log(2, tailoredJobId=str(tailored.id))
    await log(3, tailoredJobId=str(fixed.id))
    await log(4, resumeId="r2")  # no score: skipped
    got = await pm.scores([key], START, END)
    assert got[key]["tailored"] == [80] and sorted(got[key]["match"]) == [60, 70]


def _weekly(start: date, weeks: int, self_bids=0, bidder=0, self_iv=0, caller=0):
    """The first week holds the counts, the rest are empty."""
    return {
        start + timedelta(weeks=i): dict(
            selfBids=self_bids * (i == 0), bidderBids=bidder * (i == 0),
            selfInterviews=self_iv * (i == 0), callerInterviews=caller * (i == 0),
        )
        for i in range(weeks)
    }


NO_SCORES = {"tailored": [], "match": []}


def test_summarize_values():
    start = date(2026, 10, 5)
    row = pm.summarize("a1", start, 4, _weekly(start, 4, 120, 48, 8, 2), {"tailored": [80, 82], "match": [70]})
    assert row == {
        "accountId": "a1", "spanWeeks": 4, "bidsPerWeek": 42.0, "selfBidsPerWeek": 30.0, "bidderBidsPerWeek": 12.0,
        "interviewsPerWeek": 2.5, "selfInterviewsPerWeek": 2.0, "callerInterviewsPerWeek": 0.5,
        "interviewsPerBid": 0.0595, "lowVolume": False,
        "tailorScore": {"avg": 81.0, "n": 2}, "matchScore": {"avg": 70.0, "n": 1},
    }


def test_summarize_zero_bids():
    start = date(2026, 10, 12)
    row = pm.summarize("a1", start, 2, _weekly(start, 2, self_iv=2), NO_SCORES)
    assert row["interviewsPerWeek"] == 1.0 and row["interviewsPerBid"] is None and row["lowVolume"] is True
    assert row["tailorScore"] == {"avg": None, "n": 0}


def test_summarize_zero_weeks():
    row = pm.summarize("a1", date(2026, 10, 19), 0, {}, NO_SCORES)
    assert row["spanWeeks"] == 0 and row["interviewsPerBid"] is None
    for f in ("bidsPerWeek", "selfBidsPerWeek", "bidderBidsPerWeek", "interviewsPerWeek", "selfInterviewsPerWeek",
              "callerInterviewsPerWeek"):
        assert row[f] is None


def test_peer_average():
    start = date(2026, 10, 5)
    a = pm.summarize("a", start, 4, _weekly(start, 4, 120, 48, 8, 2), {"tailored": [80], "match": []})
    b = pm.summarize("b", start, 4, _weekly(start, 4), {"tailored": [90, 100], "match": []})
    avg = pm.peer_average([a, b])
    assert avg["bidsPerWeek"] == 21.0  # (42 + 0) / 2
    assert avg["interviewsPerBid"] == 0.0595  # b has none: only a counts
    assert avg["tailorScore"] == {"avg": 87.5, "n": 3}  # mean of 80 and 95
    assert avg["matchScore"] == {"avg": None, "n": 0}
    assert pm.peer_average([]) is None


async def test_collect_keeps_the_profiles_own_weeks(db):
    user, acc = await _setup()
    key = str(acc.id)
    await _plan(user, date(2026, 10, 20), {key: 5})
    start, n_weeks, weekly, scores = (await pm.collect([acc], 4, date(2026, 11, 3)))[key]
    assert (start, n_weeks) == (date(2026, 10, 5), 4)
    assert weekly[MON]["selfBids"] == 5 and weekly[date(2026, 10, 5)]["selfBids"] == 0
    assert scores == NO_SCORES


async def test_collect_clips_scores_to_each_span(db):
    user = await UserExtra(name="Ana", email="ana@x.io").insert()
    old = await Account(name="Old", createdBy=user.id, createdAt=datetime(2026, 1, 1)).insert()
    new = await Account(name="New", createdBy=user.id, createdAt=datetime(2026, 10, 20)).insert()  # span starts Oct 19

    async def log(i, acc, day):
        await JobApplicationLog(
            userId=user.id, url=f"u{i}", accountId=str(acc.id), fileKey=f"f{i}", resumeId="r", appliedAt=day,
            score={"total": 70 + i, "band": "ok"},
        ).insert()

    await log(1, new, datetime(2026, 10, 14, 12))  # before the new profile's span
    await log(2, new, datetime(2026, 10, 21, 12))
    await log(3, old, datetime(2026, 10, 14, 12))
    out = await pm.collect([old, new], 4, date(2026, 11, 3))
    assert out[str(new.id)][3] == {"tailored": [], "match": [72]}
    assert out[str(old.id)][3] == {"tailored": [], "match": [73]}
