"""Pay weeks and the week board (/bids/week, /bids?week=) against an in-memory MongoDB (mongomock-motor)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date, datetime, timedelta, timezone

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids.storage import BidStorageError
from app.bids.windows import schedule_from_settings
from app.models import Account, ResumeApplication, UserExtra
from app.models.bid import Bid
from app.models.bid_report import BidReport
from app.models.bidder import Bidder
from tests_unit.test_bids_windows import CHI, sched_off, sched_on

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

def chi(month, day, hh, mm=0) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=CHI)


def z(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class FakeStorage:
    """Folder listings: each folder URL has job-folder timestamps, or raises."""

    def __init__(self, folders=None, errors=None):
        self.folders, self.errors, self.calls = folders or {}, errors or {}, []

    async def count_windows(self, folder_url, windows):
        self.calls.append((folder_url, list(windows)))
        if folder_url in self.errors:
            raise BidStorageError(self.errors[folder_url])
        times = self.folders.get(folder_url, [])
        return [sum(1 for t in times if s <= t < e) for s, e in windows]

    async def count_bids(self, folder_url, start, end):
        return (await self.count_windows(folder_url, [(start, end)]))[0]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["week-unit"]
    await init_beanie(database=database, document_models=[Bidder, Bid, BidReport, Account, ResumeApplication, UserExtra])
    return database


@pytest.fixture
def records_start(monkeypatch):
    from app.bids import records
    from app.config import get_settings

    def set_start(value):
        settings = get_settings().model_copy(update={"BID_RECORDS_START": value})
        monkeypatch.setattr(records, "get_settings", lambda: settings)

    set_start("2099-01-01")  # folders everywhere unless a test sets its own start
    return set_start


async def make_user(name, role="staff") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def make_bidder(owner, name="Ana", archived=False, rate=0.5) -> Bidder:
    return await Bidder(
        createdBy=owner.id, name=name, rate=rate, folder=name.lower(),
        screenshotFolderUrl=f"s3://shots/{name.lower()}/",
        archivedAt=datetime.utcnow() if archived else None,
    ).insert()


async def add_bid(bidder, at, status="pending") -> Bid:
    naive = at.astimezone(timezone.utc).replace(tzinfo=None)
    job = f"job{int(naive.timestamp())}{status}"
    return await Bid(
        bidderId=bidder.id, ownerId=bidder.createdBy, jobKey=f"{bidder.folder}/x/{job}",
        jobUrl=f"https://jobs.example/{job}", jobTitle=job, firstAt=naive, status=status, screenshots=[],
    ).insert()


def by_day(row) -> dict:
    return {d["day"]: d for d in row["days"]}


# --- pay_week -------------------------------------------------------------------------------------------


def test_pay_week_for_a_saturday():
    from app.bids.week import pay_week

    pw = pay_week(schedule_from_settings(), chi(10, 20, 9), date(2026, 10, 10))
    assert (pw.start, pw.end) == (z("2026-10-03T21:00:00Z"), z("2026-10-10T21:00:00Z"))
    assert [d.key for d in pw.days] == [f"2026-10-{n:02d}" for n in range(4, 11)]
    assert pw.days[0].start == z("2026-10-03T23:30:00Z")
    assert all(d.end - d.start == timedelta(hours=24) for d in pw.days)


def test_pay_week_default_is_the_week_containing_now():
    from app.bids.week import pay_week

    pw = pay_week(schedule_from_settings(), chi(10, 10, 17))
    assert pw.end == chi(10, 17, 16).astimezone(timezone.utc)
    assert pw.days[-1].key == "2026-10-17"


def test_pay_week_across_dst():
    from app.bids.week import pay_week

    pw = pay_week(schedule_from_settings(), chi(10, 20, 9), date(2026, 11, 4))
    assert (pw.start, pw.end) == (z("2026-10-31T21:00:00Z"), z("2026-11-07T22:00:00Z"))
    assert [d.key for d in pw.days] == [f"2026-11-{n:02d}" for n in range(1, 8)]
    first = pw.days[0]
    assert (first.start, first.end) == (z("2026-10-31T23:30:00Z"), z("2026-11-02T00:30:00Z"))
    assert first.end - first.start == timedelta(hours=25)
    assert all(d.end - d.start == timedelta(hours=24) for d in pw.days[1:])


# --- pay_week across the schedule switch (Sun 2026-11-22 20:00: daily 18:30 -> 18:00, weekly Sat 16:00 -> Sun 20:00)


def test_the_long_week_at_the_switch():
    from app.bids.week import pay_week, week_label

    pw = pay_week(sched_on, chi(11, 18, 9))
    assert (pw.start, pw.end) == (z("2026-11-14T22:00:00Z"), z("2026-11-23T02:00:00Z"))
    assert pw.cutoff == chi(11, 22, 20)
    assert [d.key for d in pw.days] == [f"2026-11-{n:02d}" for n in range(15, 23)]
    assert week_label(pw) == "Week ending Sun Nov 22"
    last = pw.days[-1]
    assert (last.start, last.end) == (chi(11, 21, 18, 30), chi(11, 22, 18, 30))


def test_the_first_sunday_week():
    from app.bids.week import pay_week, week_label

    pw = pay_week(sched_on, chi(11, 25, 9))
    assert (pw.start, pw.end) == (chi(11, 22, 20), chi(11, 29, 20))
    assert [d.key for d in pw.days] == [f"2026-11-{n:02d}" for n in range(23, 30)]
    assert week_label(pw) == "Week ending Sun Nov 29"
    first = pw.days[0]
    assert (first.start, first.end) == (chi(11, 22, 18, 30), chi(11, 23, 18))
    assert all((d.start, d.end) == (chi(11, n - 1, 18), chi(11, n, 18)) for d, n in zip(pw.days[1:], range(24, 30)))


def test_week_param_picks_the_right_week():
    from app.bids.week import pay_week

    now = chi(11, 25, 9)
    for day in (date(2026, 11, 21), date(2026, 11, 22)):
        pw = pay_week(sched_on, now, day)
        assert (pw.start, pw.end) == (z("2026-11-14T22:00:00Z"), z("2026-11-23T02:00:00Z")), day
    pw = pay_week(sched_on, now, date(2026, 11, 23))
    assert pw.cutoff == chi(11, 29, 20)
    assert pw.days[0].key == "2026-11-23"


def test_old_weeks_are_unchanged():
    from app.bids.week import pay_week

    now = chi(11, 25, 9)
    anchors = [date(2026, 10, 3) + timedelta(days=7 * i) for i in range(7)]  # Saturdays Oct 3 .. Nov 14
    assert anchors[-1] == date(2026, 11, 14)
    for anchor in anchors:
        on, off = pay_week(sched_on, now, anchor), pay_week(sched_off, now, anchor)
        assert (on.start, on.end, on.cutoff) == (off.start, off.end, off.cutoff), anchor
        assert on.cutoff.tzinfo is off.cutoff.tzinfo
        assert [(d.key, d.start, d.end) for d in on.days] == [(d.key, d.start, d.end) for d in off.days], anchor
        assert len(on.days) == 7


def test_dst_week_after_the_switch():
    from app.bids.week import pay_week

    pw = pay_week(sched_on, datetime(2027, 3, 10, 9, tzinfo=CHI))
    assert pw.cutoff == datetime(2027, 3, 14, 20, tzinfo=CHI)
    assert (pw.cutoff.hour, pw.cutoff.minute) == (20, 0)
    assert pw.end == z("2027-03-15T01:00:00Z")
    assert [d.key for d in pw.days] == [f"2027-03-{n:02d}" for n in range(8, 15)]
    assert pw.end - pw.start == timedelta(hours=167)


# --- week_board ---------------------------------------------------------------------------------------


async def test_week_board_counts_days_and_week_from_records(db, records_start):
    from app.bids.week import pay_week, week_board

    records_start("2026-09-01")
    owner = await make_user("Pedro")
    ana, bob = await make_bidder(owner, "Ana"), await make_bidder(owner, "Bob")
    await add_bid(ana, chi(10, 3, 15), "approved")   # before the pay week
    await add_bid(ana, chi(10, 3, 17), "approved")   # in the pay week, but in Saturday Oct 3's day (last week's)
    await add_bid(ana, chi(10, 3, 19), "approved")   # Oct 4's day (starts Sat 18:30)
    await add_bid(ana, chi(10, 4, 10), "approved")
    await add_bid(ana, chi(10, 4, 11), "pending")
    await add_bid(ana, chi(10, 5, 9), "rejected")
    await add_bid(ana, chi(10, 10, 17), "approved")  # Saturday's day, after the weekly cutoff
    await add_bid(bob, chi(10, 6, 10), "pending")
    sched, storage = schedule_from_settings(), FakeStorage()

    board = await week_board([ana, bob], pay_week(sched, chi(10, 11, 12), date(2026, 10, 10)), chi(10, 11, 12), sched, storage)

    assert storage.calls == []
    a, b = board["bidders"]
    assert (a["id"], a["name"], a["rate"], a["status"]) == (str(ana.id), "Ana", 0.5, "invited")
    days = by_day(a)
    assert {k: (d["approved"], d["pending"], d["rejected"]) for k, d in days.items() if any(
        (d["approved"], d["pending"], d["rejected"]))} == {
        "2026-10-04": (2, 1, 0), "2026-10-05": (0, 0, 1), "2026-10-10": (1, 0, 0)}
    assert all(d["source"] == "records" and d["error"] is None for d in a["days"])
    assert a["week"] == {"approved": 3, "pending": 1, "rejected": 1, "pay": 1.5, "toReview": 1,
                         "source": "records", "error": None}
    assert (by_day(b)["2026-10-06"]["pending"], b["week"]["toReview"]) == (1, 1)

    t = board["totals"]
    assert (t["approved"], t["pending"], t["rejected"], t["pay"], t["toReview"]) == (3, 2, 1, 1.5, 2)
    tdays = {d["day"]: (d["approved"], d["pending"], d["rejected"]) for d in t["days"]}
    assert tdays["2026-10-04"] == (2, 1, 0) and tdays["2026-10-06"] == (0, 1, 0) and tdays["2026-10-10"] == (1, 0, 0)


async def test_week_board_folder_days_before_records_start(db, records_start):
    from app.bids.week import pay_week, week_board

    records_start("2026-10-07")  # days starting Oct 7 18:30 onward use records
    owner = await make_user("Pedro")
    ana = await make_bidder(owner, "Ana")
    await add_bid(ana, chi(10, 8, 10), "approved")
    storage = FakeStorage(folders={ana.screenshotFolderUrl: [
        chi(10, 4, 10), chi(10, 4, 12), chi(10, 6, 9), chi(10, 9, 10)]})
    sched = schedule_from_settings()

    board = await week_board([ana], pay_week(sched, chi(10, 11, 12), date(2026, 10, 10)), chi(10, 11, 12), sched, storage)

    days = by_day(board["bidders"][0])
    assert {k: (d["approved"], d["pending"], d["rejected"], d["source"]) for k, d in days.items()} == {
        "2026-10-04": (2, None, None, "folders"), "2026-10-05": (0, None, None, "folders"),
        "2026-10-06": (1, None, None, "folders"), "2026-10-07": (0, None, None, "folders"),
        "2026-10-08": (1, 0, 0, "records"), "2026-10-09": (0, 0, 0, "records"), "2026-10-10": (0, 0, 0, "records")}
    assert board["bidders"][0]["week"] == {"approved": 4, "pending": None, "rejected": None, "pay": 2.0,
                                           "toReview": None, "source": "folders", "error": None}
    assert len(storage.calls) == 1  # one listing for all of the bidder's folder windows


async def test_week_board_storage_error_is_per_bidder(db, records_start):
    from app.bids.week import pay_week, week_board

    owner = await make_user("Pedro")
    ana, bob = await make_bidder(owner, "Ana"), await make_bidder(owner, "Bob")
    storage = FakeStorage(folders={bob.screenshotFolderUrl: [chi(10, 5, 10)]},
                          errors={ana.screenshotFolderUrl: "AccessDenied"})
    sched = schedule_from_settings()

    board = await week_board([ana, bob], pay_week(sched, chi(10, 11, 12), date(2026, 10, 10)), chi(10, 11, 12), sched, storage)

    a, b = board["bidders"]
    assert all(d["approved"] is None and d["error"] == "AccessDenied" for d in a["days"])
    assert (a["week"]["approved"], a["week"]["pay"], a["week"]["error"]) == (None, None, "AccessDenied")
    assert (by_day(b)["2026-10-05"]["approved"], b["week"]["approved"], b["week"]["pay"]) == (1, 1, 0.5)
    assert board["totals"]["approved"] == 1


async def test_week_board_marks_today_and_future(db, records_start):
    from app.bids.week import pay_week, week_board

    owner = await make_user("Pedro")
    ana = await make_bidder(owner, "Ana")
    now, sched = chi(10, 7, 12), schedule_from_settings()
    storage = FakeStorage(folders={ana.screenshotFolderUrl: [chi(10, 7, 9)]})

    board = await week_board([ana], pay_week(sched, now), now, sched, storage)

    week = board["week"]
    assert (week["label"], week["timezone"]) == ("Week ending Sat Oct 10", "America/Chicago")
    assert (week["start"], week["end"]) == ("2026-10-03T21:00:00Z", "2026-10-10T21:00:00Z")
    flags = {d["day"]: (d["isToday"], d["isFuture"]) for d in week["days"]}
    assert [k for k, (today, _) in flags.items() if today] == ["2026-10-07"]
    assert [k for k, (_, future) in flags.items() if future] == ["2026-10-08", "2026-10-09", "2026-10-10"]
    days = by_day(board["bidders"][0])
    assert days["2026-10-07"]["approved"] == 1
    assert all((days[k]["approved"], days[k]["pending"], days[k]["rejected"]) == (0, 0, 0)
               for k in ("2026-10-08", "2026-10-09", "2026-10-10"))
    [(_, windows)] = storage.calls
    assert all(s <= now for s, _ in windows)  # future days are not listed


# --- routes -------------------------------------------------------------------------------------------


@pytest.fixture
def make_client(db, monkeypatch, records_start):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import bids

    monkeypatch.setattr(bids, "get_bid_storage", lambda: FakeStorage())
    monkeypatch.setattr(bids, "FOLDER_CACHE", bids.TtlCache(timedelta(minutes=5)))
    monkeypatch.setattr(bids, "_now", lambda: chi(10, 11, 12).astimezone(timezone.utc))
    records_start("2026-09-01")

    def make(user):
        app = FastAPI()
        app.include_router(bids.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


async def test_week_endpoint_access(make_client):
    pedro, maria, admin = await make_user("Pedro"), await make_user("Maria"), await make_user("Root", role="admin")
    await make_bidder(pedro, "Ana")
    await make_bidder(maria, "Zed")

    async def names(user):
        r = await make_client(user).get("/bids/week", params={"week": "2026-10-10"})
        assert r.status_code == 200
        return [b["name"] for b in r.json()["bidders"]]

    assert await names(pedro) == ["Ana"]
    assert await names(maria) == ["Zed"]
    assert await names(admin) == ["Ana", "Zed"]


async def test_week_rows_name_the_owner(make_client):
    pedro, admin = await make_user("Pedro"), await make_user("Root", role="admin")
    await make_bidder(pedro, "Ana")
    r = await make_client(admin).get("/bids/week", params={"week": "2026-10-10"})
    [row] = r.json()["bidders"]
    assert (row["ownerId"], row["ownerName"]) == (str(pedro.id), "Pedro")


async def test_week_endpoint_owner_filter(make_client):
    pedro, maria, admin = await make_user("Pedro"), await make_user("Maria"), await make_user("Root", role="admin")
    await make_bidder(pedro, "Ana")
    await make_bidder(maria, "Zed")

    async def names(user, owner):
        r = await make_client(user).get("/bids/week", params={"week": "2026-10-10", "ownerId": owner})
        assert r.status_code == 200, r.text
        return [b["name"] for b in r.json()["bidders"]]

    assert await names(admin, str(maria.id)) == ["Zed"]
    assert await names(admin, str(pedro.id)) == ["Ana"]
    # Staff only ever see their own bidders: another owner's id narrows to nothing, never widens.
    assert await names(pedro, str(maria.id)) == []
    assert await names(pedro, str(pedro.id)) == ["Ana"]


async def test_week_endpoint_bad_owner_id_is_400(make_client):
    admin = await make_user("Root", role="admin")
    r = await make_client(admin).get("/bids/week", params={"week": "2026-10-10", "ownerId": "nope"})
    assert r.status_code == 400


async def test_week_endpoint_include_archived(make_client):
    pedro = await make_user("Pedro")
    await make_bidder(pedro, "Ana")
    await make_bidder(pedro, "Old", archived=True)
    client = make_client(pedro)

    shown = (await client.get("/bids/week", params={"week": "2026-10-10"})).json()["bidders"]
    assert [b["name"] for b in shown] == ["Ana"]
    shown = (await client.get("/bids/week", params={"week": "2026-10-10", "includeArchived": "true"})).json()["bidders"]
    assert [(b["name"], b["status"]) for b in shown] == [("Ana", "invited"), ("Old", "archived")]


@pytest.mark.parametrize("week", ["2026-13-01", "abc", "2026-1-5", "0001-01-01", "9999-12-31"])
async def test_invalid_week_is_400(make_client, week):
    client = make_client(await make_user("Pedro"))
    r = await client.get("/bids/week", params={"week": week})
    assert (r.status_code, r.json()["detail"]) == (400, "invalid week")
    r = await client.get("/bids", params={"week": week})
    assert (r.status_code, r.json()["detail"]) == (400, "invalid week")


async def test_list_bids_week_filter(make_client):
    pedro = await make_user("Pedro")
    ana = await make_bidder(pedro, "Ana")
    inside = [await add_bid(ana, chi(10, 3, 17)), await add_bid(ana, chi(10, 9, 10), "approved")]
    await add_bid(ana, chi(10, 3, 15))
    await add_bid(ana, chi(10, 10, 17))

    r = await make_client(pedro).get("/bids", params={"week": "2026-10-07"})

    assert r.status_code == 200
    body = r.json()
    assert sorted(b["id"] for b in body["bids"]) == sorted(str(b.id) for b in inside)
    assert (body["start"], body["end"]) == ("2026-10-03T21:00:00Z", "2026-10-10T21:00:00Z")
    assert body["week"] == "2026-10-10"
    s = body["summary"][str(ana.id)]
    assert (s["approved"], s["pending"]) == (1, 1)


async def test_list_bids_day_and_week_is_400(make_client):
    r = await make_client(await make_user("Pedro")).get("/bids", params={"day": "2026-10-05", "week": "2026-10-10"})
    assert (r.status_code, r.json()["detail"]) == (400, "use day or week, not both")
