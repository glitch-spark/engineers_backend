"""`q` on the three interview feeds (list, calendar, caller schedule): a case-insensitive, literal match on the
company name or the applied position. Real routes over mongomock-motor."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timezone
from urllib.parse import quote

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, ResumeApplication, UserExtra
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent
from app.models.interview_question import InterviewQuestion

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
RANGE = "from=2026-10-01T00:00:00Z&to=2026-10-20T00:00:00Z"
URLS = {
    "list": "/interviews",
    "rounds": f"/interviews/rounds?{RANGE}",
    "caller": "/interviews/caller-rounds",
}
FEEDS = ["list", "rounds", "caller"]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["interviews-search-unit"]
    await init_beanie(
        database=database,
        document_models=[Interview, UserExtra, Account, InterviewQuestion, ResumeApplication],
    )
    return database


@pytest.fixture
def client(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import interviews

    def make(user):
        app = FastAPI()
        app.include_router(interviews.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


async def make_user(name, role="staff") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def make_interview(owner, company, position):
    """An interview with one caller round on Oct 7 (so every feed returns it)."""
    acc = await Account(name="Alex", region="EU", country="PT", createdBy=owner.id).insert()
    rounds = [
        InterviewStageEvent(
            stage="intro", at=START, scheduledAt=START, endsAt=START.replace(hour=16), status="scheduled",
            caller=InterviewCaller(enabled=True, callerName="Cal", callerKind="external", startsAt=START, method="video"),
        ),
    ]
    return await Interview(
        accountId=acc.id, createdBy=owner.id, scheduledAt=START, companyName=company, appliedPosition=position,
        stageHistory=[r.model_dump() for r in rounds], stage="intro", status="scheduled",
    ).insert()


async def seed(owner):
    await make_interview(owner, "ACME Corp", "Engineer")  # company matches
    await make_interview(owner, "Globex", "Acme lead")  # position matches
    await make_interview(owner, "Initech", "Developer")  # neither
    await make_interview(owner, "R&D (Labs) + Co", "C++ [Senior]. Dev")  # regex characters


def companies(feed, body):
    if feed == "list":
        return sorted(i["companyName"] for i in body["interviews"])
    return sorted({r["companyName"] for r in body["rounds"]})


def url(feed, q):
    sep = "&" if "?" in URLS[feed] else "?"
    return f"{URLS[feed]}{sep}q={quote(q)}"


@pytest.mark.parametrize("feed", FEEDS)
async def test_q_matches_company_or_position_case_insensitively(client, feed):
    me = await make_user("Mia")
    await seed(me)
    async with client(me) as c:
        for q in ("acme", "ACME", "AcMe"):
            body = (await c.get(url(feed, q))).json()
            assert companies(feed, body) == ["ACME Corp", "Globex"], q


@pytest.mark.parametrize("feed", FEEDS)
async def test_q_matches_part_of_a_word_and_ignores_other_fields(client, feed):
    me = await make_user("Mia")
    await seed(me)
    async with client(me) as c:
        assert companies(feed, (await c.get(url(feed, "nitec"))).json()) == ["Initech"]
        assert companies(feed, (await c.get(url(feed, "devel"))).json()) == ["Initech"]
        assert companies(feed, (await c.get(url(feed, "nothing-like-this"))).json()) == []
        # the account name is not searched
        assert companies(feed, (await c.get(url(feed, "alex"))).json()) == []


@pytest.mark.parametrize("feed", FEEDS)
async def test_q_regex_characters_are_literal(client, feed):
    me = await make_user("Mia")
    await seed(me)
    async with client(me) as c:
        # "." and ".*" would match everything if they were regex.
        assert companies(feed, (await c.get(url(feed, "."))).json()) == ["R&D (Labs) + Co"]
        assert companies(feed, (await c.get(url(feed, ".*"))).json()) == []
        assert companies(feed, (await c.get(url(feed, "(labs)"))).json()) == ["R&D (Labs) + Co"]
        assert companies(feed, (await c.get(url(feed, "c++ [senior]"))).json()) == ["R&D (Labs) + Co"]
        # an unbalanced bracket / paren must not blow up the regex engine: it is just text
        for q in ("[", "("):
            r = await c.get(url(feed, q))
            assert r.status_code == 200, q
            assert companies(feed, r.json()) == ["R&D (Labs) + Co"], q
        for q in ("\\", "a|b", "^Acme", "Corp$"):
            r = await c.get(url(feed, q))
            assert r.status_code == 200, q
            assert companies(feed, r.json()) == [], q


@pytest.mark.parametrize("feed", FEEDS)
async def test_empty_q_is_no_filter(client, feed):
    me = await make_user("Mia")
    await seed(me)
    async with client(me) as c:
        everything = companies(feed, (await c.get(URLS[feed])).json())
        assert len(everything) == 4
        assert companies(feed, (await c.get(url(feed, ""))).json()) == everything
        assert companies(feed, (await c.get(url(feed, "   "))).json()) == everything


async def test_q_combines_with_other_filters_and_the_date_range(client):
    me = await make_user("Mia")
    other = await make_user("Omar")
    await seed(me)
    await make_interview(other, "Acme West", "Dev")
    async with client(me) as c:
        body = (await c.get(f"/interviews?q=acme&creatorId={me.id}&{RANGE}")).json()
        assert companies("list", body) == ["ACME Corp", "Globex"]
        body = (await c.get("/interviews?q=acme&from=2026-11-01T00:00:00Z")).json()
        assert companies("list", body) == []
        body = (await c.get(f"/interviews/rounds?q=acme&creatorId={other.id}&{RANGE}")).json()
        assert companies("rounds", body) == ["Acme West"]


@pytest.mark.parametrize("feed", FEEDS)
async def test_q_does_not_change_the_trim(client, feed):
    """Filtering sees the full row; what a non-owner gets back is still the summary only."""
    owner = await make_user("Owen")
    viewer = await make_user("Vera")
    iv = await make_interview(owner, "ACME Corp", "Engineer")
    iv.note = "TOPSECRET-NOTE"
    iv.jobUrl = "https://TOPSECRET-URL.example/job"
    await iv.save()
    async with client(viewer) as c:
        r = await c.get(url(feed, "acme"))
    assert r.status_code == 200
    assert "TOPSECRET" not in r.text
    body = r.json()
    rows = body["interviews"] if feed == "list" else list(body["interviews"].values())
    assert [i["companyName"] for i in rows] == ["ACME Corp"]
    assert rows[0]["summaryOnly"] is True
    assert "note" not in rows[0] and "jobUrl" not in rows[0]
