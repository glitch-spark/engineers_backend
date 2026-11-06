import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids.performance import day_counts, performance, previous_range, rank, streaks, week_columns
from app.bids.windows import schedule_from_settings
from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]
CHI = ZoneInfo("America/Chicago")

D = lambda m, d: date(2026, m, d)


def test_week_columns():
    cols = week_columns(D(10, 1), D(10, 31))
    assert len(cols) == 5
    assert cols[0] == {"start": "2026-10-01", "end": "2026-10-04", "partial": True}
    assert cols[1] == {"start": "2026-10-05", "end": "2026-10-11", "partial": False}
    assert cols[-1] == {"start": "2026-10-26", "end": "2026-10-31", "partial": True}


def test_streaks_weekend_does_not_break():
    r = streaks({D(10, 23): 50, D(10, 26): 60, D(10, 27): 55}, D(10, 19), D(10, 30), D(11, 4))
    assert (r["longestStreak"], r["hitDays"], r["weekdays"]) == (3, 3, 10)


def test_streaks_49_breaks():
    c = dict(zip([D(10, d) for d in range(19, 24)], [50, 50, 49, 50, 50]))
    r = streaks(c, D(10, 19), D(10, 23), D(11, 4))
    assert r == {"longestStreak": 2, "currentStreak": 2, "hitDays": 4, "weekdays": 5}


def test_streaks_today():
    c = {D(11, 2): 60, D(11, 3): 60, D(11, 4): 30}
    r = streaks(c, D(11, 2), D(11, 8), D(11, 4))
    assert (r["weekdays"], r["currentStreak"]) == (2, 2)
    c[D(11, 4)] = 50
    r = streaks(c, D(11, 2), D(11, 8), D(11, 4))
    assert (r["weekdays"], r["currentStreak"]) == (3, 3)


def test_streaks_clip_to_active_days():
    assert streaks({}, D(10, 21), D(10, 30), D(11, 4))["weekdays"] == 8


def test_streaks_empty_range():
    assert streaks({}, D(10, 5), D(10, 1), D(11, 4)) == {"longestStreak": 0, "currentStreak": 0, "hitDays": 0, "weekdays": 0}


def test_previous_range():
    assert previous_range("week", D(10, 19), D(10, 25)) == (D(10, 12), D(10, 18))
    assert previous_range("month", D(10, 1), D(10, 31)) == (D(9, 1), D(9, 30))
    assert previous_range("month", D(11, 1), D(11, 4)) == (D(10, 1), D(10, 4))
    assert previous_range("month", D(3, 1), D(3, 31)) == (D(2, 1), D(2, 28))
    assert previous_range("custom", D(9, 1), D(10, 31)) == (D(7, 2), D(8, 31))


def row(id, name, total=0, approved=0, streak=0, rate=0.0, interviews=0, per100=None):
    return {"id": id, "name": name, "total": total, "approved": approved, "longestStreak": streak, "hitRate": rate,
            "interviews": interviews, "per100": per100}


def test_rank():
    rows = [
        row("a", "Zed", 200, 150, 5, 0.5, 4, 2.0), row("b", "Amy", 200, 150, 5, 0.5, 4, 2.0),
        row("c", "Cy", 200, 160, 5, 0.6, 3, 1.5), row("d", "Di", 150, 100, 7, 0.4, 3, 2.0),
        row("e", "Ed", 99, 99, 0, 0.1, 5, 5.0), row("f", "Flo", 0),
    ]
    r = rank(rows)
    assert r["bids"] == ["c", "b", "a"]
    assert r["streak"] == ["d", "c", "a"]  # a, b tie fully: input order
    assert r["interviews"] == ["e", "b", "a"]
    assert r["conversion"] == ["b", "a", "d"]  # e has < 100 bids; Amy before Zed


# --- loaders and the response ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["performance-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, Interview, InterviewQuestion])
    return database


@pytest.fixture
def records_start(monkeypatch):
    from app.bids import records
    from app.config import get_settings

    def set_start(value):
        settings = get_settings().model_copy(update={"BID_RECORDS_START": value})
        monkeypatch.setattr(records, "get_settings", lambda: settings)

    set_start(None)
    return set_start


def chi(month, day, hh, mm=0, ss=0) -> datetime:
    return datetime(2026, month, day, hh, mm, ss, tzinfo=CHI)


async def make_user(name="Pedro") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role="staff").insert()


async def make_bidder(owner, name="Ana", profile=None, archived_at=None) -> Bidder:
    return await Bidder(
        createdBy=owner.id, name=name, rate=0.5, folder=name.lower(), screenshotFolderUrl=f"s3://shots/{name.lower()}/",
        profileId=profile.id if profile else None, archivedAt=archived_at, createdAt=datetime(2026, 1, 1),
    ).insert()


_n = iter(range(10**6))


async def add_bid(bidder, at, status="pending", n=1) -> None:
    naive = at.astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        job = f"job{next(_n)}"
        await Bid(bidderId=bidder.id, ownerId=bidder.createdBy, jobKey=f"{bidder.folder}/x/{job}",
                  jobUrl=f"https://jobs.example/{job}", jobTitle=job, firstAt=naive, status=status,
                  screenshots=[]).insert()


async def make_round(profile, owner, at):
    await Interview(
        accountId=profile.id, createdBy=owner.id, scheduledAt=at, companyName="Acme",
        stageHistory=[{"stage": "intro", "at": at, "scheduledAt": at, "status": "completed"}],
    ).insert()


async def run(bidders, start=date(2026, 10, 1), end=date(2026, 10, 31), period="month", today=date(2026, 11, 4)):
    return await performance(bidders, period, start, end, "Oct 2026", today)


async def test_day_counts_cutoff_and_dst(db):
    owner = await make_user()
    b = await make_bidder(owner)
    await add_bid(b, chi(10, 20, 18, 29, 59))
    await add_bid(b, chi(10, 20, 18, 30), "approved")
    await add_bid(b, chi(10, 31, 18, 30))  # Sat, CDT
    await add_bid(b, chi(11, 1, 18, 29), "rejected")  # Sun, CST after the change
    await add_bid(b, chi(11, 1, 18, 30), "approved")
    got = await day_counts([b], date(2026, 10, 20), date(2026, 11, 2), schedule_from_settings())
    k = str(b.id)
    assert got[(k, date(2026, 10, 20))] == (0, 1, 0)
    assert got[(k, date(2026, 10, 21))] == (1, 0, 0)
    assert got[(k, date(2026, 11, 1))] == (0, 1, 1)
    assert got[(k, date(2026, 11, 2))] == (1, 0, 0)


async def test_performance_counts(db, records_start):
    owner = await make_user()
    a, b = await make_bidder(owner, "Ana"), await make_bidder(owner, "Bo")
    await add_bid(a, chi(10, 6, 10), "approved", 60)
    await add_bid(a, chi(10, 6, 11), "pending", 40)
    await add_bid(a, chi(10, 6, 12), "rejected", 5)
    await add_bid(b, chi(10, 13, 10), "approved", 10)
    await add_bid(a, chi(9, 29, 10), "approved", 100)  # previous month
    await add_bid(a, chi(9, 10, 10), "approved", 100)
    r = await run([a, b])
    ra = next(x for x in r["bidders"] if x["name"] == "Ana")
    assert (ra["total"], ra["approved"], ra["pending"], ra["rejected"]) == (100, 60, 40, 5)
    assert sum(ra["weekly"]) == 100 and len(ra["weekly"]) == len(r["weeks"])
    assert ra["previousTotal"] == 200 and ra["change"] == -0.5
    assert ra["ownerName"] == "Pedro" and ra["status"] == "active" and ra["profileName"] is None
    assert [x["name"] for x in r["bidders"]] == ["Ana", "Bo"]
    assert r["totals"]["total"] == 110
    assert r["top"]["bids"] == [str(a.id), str(b.id)]
    rb = r["bidders"][1]
    assert rb["previousTotal"] == 0 and rb["change"] is None
    assert r["recordsStart"] is None


async def test_change_rounds(db, records_start):
    owner = await make_user()
    a = await make_bidder(owner)
    await add_bid(a, chi(10, 6, 10), "approved", 110)
    await add_bid(a, chi(9, 8, 10), "approved", 100)
    r = await run([a])
    assert r["bidders"][0]["change"] == 0.1


async def test_records_start_clips_weekdays(db, records_start):
    records_start("2026-10-15")
    owner = await make_user()
    a = await make_bidder(owner)
    r = await run([a])
    assert r["recordsStart"] == "2026-10-15"
    assert r["bidders"][0]["weekdays"] == 12  # Oct 15-30 weekdays; Oct 31 is a Saturday


async def test_shared_profile_counted_once(db, records_start):
    owner = await make_user()
    p, q, solo = [await Account(name=n, createdBy=owner.id).insert() for n in ("P", "Q", "Solo")]
    a, b = await make_bidder(owner, "Ana", p), await make_bidder(owner, "Bo", p)
    c = await make_bidder(owner, "Cy", q)
    d = await make_bidder(owner, "Di")
    e = await make_bidder(owner, "Ed", solo)
    await make_round(p, owner, datetime(2026, 10, 7, 15))
    await make_round(p, owner, datetime(2026, 10, 8, 15))
    await DailyPlan(userId=owner.id, date=datetime(2026, 10, 9), year=2026, weekNumber=41,
                    done={"bidsSelfByProfile": {str(q.id): 3}}, loggedAt=datetime(2026, 10, 9, 20)).insert()
    r = await run([a, b, c, d, e])
    rows = {x["name"]: x for x in r["bidders"]}
    assert rows["Ana"]["interviews"] == rows["Bo"]["interviews"] == 2
    assert rows["Ana"]["sharedProfile"] and rows["Bo"]["sharedProfile"]
    assert rows["Cy"]["sharedProfile"] is True and rows["Cy"]["interviews"] == 0
    assert rows["Ed"]["sharedProfile"] is False
    assert rows["Di"]["interviews"] is None and rows["Di"]["profileName"] is None
    assert rows["Ana"]["profileName"] == "P"
    assert r["totals"]["interviews"] == 2


async def test_archived_bidder_is_not_a_sharer(db, records_start):
    owner = await make_user()
    p = await Account(name="P", createdBy=owner.id).insert()
    a = await make_bidder(owner, "Ana", p)
    await make_bidder(owner, "Old", p, archived_at=datetime(2026, 10, 10))
    r = await run([a])
    assert r["bidders"][0]["sharedProfile"] is False


async def test_per100_and_last_bid(db, records_start):
    owner = await make_user()
    p = await Account(name="P", createdBy=owner.id).insert()
    low, high = await make_bidder(owner, "Low", p), await make_bidder(owner, "High")
    await add_bid(low, chi(10, 6, 10), "approved", 99)
    await add_bid(high, chi(10, 6, 10), "approved", 200)
    await add_bid(high, chi(11, 3, 9, 0), "approved")
    await make_round(p, owner, datetime(2026, 10, 7, 15))
    r = await run([low, high])
    rows = {x["name"]: x for x in r["bidders"]}
    assert rows["Low"]["per100"] is None and rows["Low"]["interviews"] == 1
    assert rows["High"]["lastBidAt"] == "2026-11-03T15:00:00Z"
    p2 = await Account(name="P2", createdBy=owner.id).insert()
    big = await make_bidder(owner, "Big", p2)
    await add_bid(big, chi(10, 6, 10), "approved", 200)
    await make_round(p2, owner, datetime(2026, 10, 7, 15))
    r = await run([big])
    assert r["bidders"][0]["per100"] == 0.5 and r["totals"]["per100"] == 0.5


async def test_days_strip_marks_each_weekday(db, records_start):
    owner = await make_user()
    a = await make_bidder(owner, "Ana")
    late = await make_bidder(owner, "Late")
    late.createdAt = datetime(2026, 11, 3, 16)  # Tue 10:00 Chicago
    await late.save()
    await add_bid(a, chi(10, 30, 10), "approved", 70)  # Fri before: outside the week
    await add_bid(a, chi(11, 2, 10), "approved", 50)
    await add_bid(a, chi(11, 3, 10), "pending", 10)
    await add_bid(a, chi(11, 3, 11), "rejected", 60)  # rejected bids never count
    await add_bid(a, chi(11, 4, 10), "pending", 20)
    await add_bid(late, chi(11, 4, 10), "approved", 55)
    r = await run([a, late], date(2026, 11, 2), date(2026, 11, 4), "week", date(2026, 11, 4))
    rows = {x["name"]: x for x in r["bidders"]}
    assert rows["Ana"]["days"] == [
        {"day": "2026-11-02", "bids": 50, "state": "hit"},
        {"day": "2026-11-03", "bids": 10, "state": "miss"},
        {"day": "2026-11-04", "bids": 20, "state": "open"},  # today, still under 50
    ]
    assert [d["state"] for d in rows["Late"]["days"]] == ["none", "miss", "hit"]  # created Tue; today hit 50+


async def test_days_strip_skips_weekends_and_archived_days(db, records_start):
    owner = await make_user()
    a = await make_bidder(owner, "Ana", archived_at=datetime(2026, 10, 7, 15))  # archived Wed Oct 7
    r = await run([a], date(2026, 10, 2), date(2026, 10, 9), "custom", date(2026, 11, 4))
    days = r["bidders"][0]["days"]
    assert [d["day"] for d in days] == ["2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
    assert [d["state"] for d in days] == ["miss", "miss", "miss", "miss", "none", "none"]


async def test_shared_with_names_the_other_bidders(db, records_start):
    owner = await make_user()
    p, q = [await Account(name=n, createdBy=owner.id).insert() for n in ("P", "Q")]
    a, b = await make_bidder(owner, "Ana", p), await make_bidder(owner, "Bo", p)
    await make_bidder(owner, "Old", p, archived_at=datetime(2026, 10, 10))
    c = await make_bidder(owner, "Cy", q)
    await DailyPlan(userId=owner.id, date=datetime(2026, 10, 9), year=2026, weekNumber=41,
                    done={"bidsSelfByProfile": {str(q.id): 3}}, loggedAt=datetime(2026, 10, 9, 20)).insert()
    r = await run([a, b, c])
    rows = {x["name"]: x for x in r["bidders"]}
    assert rows["Ana"]["sharedWith"] == ["Bo"] and rows["Bo"]["sharedWith"] == ["Ana"]
    assert rows["Ana"]["profileSelfBids"] is False
    assert rows["Cy"]["sharedWith"] == [] and rows["Cy"]["profileSelfBids"] is True
