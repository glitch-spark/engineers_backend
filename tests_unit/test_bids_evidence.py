"""Evidence shown next to a bid for review: job domain, duration, profile, resumes and tailored resume."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids.evidence import duration_sec, evidence_for, job_domain
from app.models import Account, ResumeApplication, UserExtra
from app.models.account import Resume
from app.models.bid import Bid, Screenshot
from app.models.bidder import Bidder

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

URL = "https://boards.greenhouse.io/acme/jobs/1"
T0 = datetime(2026, 10, 6, 10, 0, 0)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["evidence-unit"]
    await init_beanie(database=database, document_models=[Bidder, Bid, Account, ResumeApplication, UserExtra])
    return database


def shot(at: datetime, step: int = 1) -> Screenshot:
    return Screenshot(key=f"ana/2026-10-06/backend/job/step{step}.png", step=step, trigger="step", capturedAt=at)


def make_bid(bidder: Bidder, url=URL, shots=None) -> Bid:
    return Bid(
        bidderId=bidder.id, ownerId=bidder.createdBy, jobKey=f"ana/2026-10-06/backend/{PydanticObjectId()}",
        jobUrl=url, firstAt=T0, screenshots=shots if shots is not None else [shot(T0)],
    )


async def make_bidder(profile: Account = None) -> Bidder:
    return await Bidder(
        createdBy=PydanticObjectId(), name="Ana", rate=0.5, folder=f"ana-{PydanticObjectId()}",
        screenshotFolderUrl="s3://shots/ana/", profileId=profile.id if profile else None,
    ).insert()


async def make_profile(name="Ian Meron", resumes=("A.pdf", "B.pdf")) -> Account:
    return await Account(
        name=name, createdBy=PydanticObjectId(), resumes=[Resume(filename=f, markdown="# big markdown") for f in resumes]
    ).insert()


async def make_tailored(profile: Account, url=URL, status="completed", pdf="x.pdf", completed=T0) -> ResumeApplication:
    return await ResumeApplication(
        userId=PydanticObjectId(), accountId=profile.id, companyName="Acme", companyNameLower="acme",
        jobDescription="jd", jobUrl=url, status=status, pdfFilename=pdf, pdfData="BASE64" * 100,
        completedAt=completed if status == "completed" else None,
    ).insert()


def test_job_domain():
    assert job_domain("https://www.boards.greenhouse.io/acme/jobs/1") == "boards.greenhouse.io"
    assert job_domain("https://jobs.lever.co/acme?x=1") == "jobs.lever.co"
    assert job_domain(None) is None
    assert job_domain("") is None


async def test_duration_sec(db):
    two = Bid(
        bidderId=PydanticObjectId(), ownerId=PydanticObjectId(), jobKey="k", firstAt=T0,
        screenshots=[shot(T0 + timedelta(minutes=4, seconds=30), 2), shot(T0, 1)],  # stored out of order
    )
    one = Bid(bidderId=PydanticObjectId(), ownerId=PydanticObjectId(), jobKey="k", firstAt=T0, screenshots=[shot(T0)])
    none = Bid(bidderId=PydanticObjectId(), ownerId=PydanticObjectId(), jobKey="k", firstAt=T0)
    assert duration_sec(two) == 270
    assert duration_sec(one) == 0
    assert duration_sec(none) == 0


async def test_evidence_for_profile_resumes_and_tailored(db):
    profile = await make_profile()
    bidder = await make_bidder(profile)
    await make_tailored(profile, pdf="old.pdf", completed=T0)
    await make_tailored(profile, pdf="new.pdf", completed=T0 + timedelta(hours=1))
    await make_tailored(profile, pdf="mid.pdf", completed=T0 + timedelta(minutes=30))  # stored last, still not newest
    await make_tailored(profile, pdf="failed.pdf", status="failed")  # never wins, even though it is newest
    await make_tailored(profile, url="https://other.example/1", pdf="other-job.pdf", completed=T0 + timedelta(hours=2))
    other_profile = await make_profile("Someone Else", ("C.pdf",))
    await make_tailored(other_profile, pdf="other-profile.pdf", completed=T0 + timedelta(hours=3))

    bid = await make_bid(bidder).insert()
    evidence = await evidence_for([bid], {str(bidder.id): bidder})
    assert evidence == {
        str(bid.id): {"profileName": "Ian Meron", "resumeNames": ["A.pdf", "B.pdf"], "tailoredResumeName": "new.pdf"}
    }


async def test_evidence_without_profile_or_url(db):
    profile = await make_profile()
    await make_tailored(profile)
    bare = await make_bidder()
    assigned = await make_bidder(profile)
    no_profile = await make_bid(bare).insert()
    no_url = await make_bid(assigned, url=None).insert()
    no_match = await make_bid(assigned, url="https://nothing.example/").insert()
    evidence = await evidence_for([no_profile, no_url, no_match], {str(bare.id): bare, str(assigned.id): assigned})
    assert evidence[str(no_profile.id)] == {"profileName": None, "resumeNames": [], "tailoredResumeName": None}
    assert evidence[str(no_url.id)] == {
        "profileName": "Ian Meron", "resumeNames": ["A.pdf", "B.pdf"], "tailoredResumeName": None,
    }
    assert evidence[str(no_match.id)]["tailoredResumeName"] is None


async def test_evidence_for_nothing_and_for_a_deleted_profile(db):
    assert await evidence_for([], {}) == {}
    gone = await Bidder(
        createdBy=PydanticObjectId(), name="Ana", rate=0.5, screenshotFolderUrl="s3://x/", profileId=PydanticObjectId()
    ).insert()
    bid = await make_bid(gone).insert()
    assert (await evidence_for([bid], {str(gone.id): gone}))[str(bid.id)] == {
        "profileName": None, "resumeNames": [], "tailoredResumeName": None,
    }


async def test_evidence_runs_one_query_per_collection(db, monkeypatch):
    profile = await make_profile()
    bidders = [await make_bidder(profile) for _ in range(3)]
    bids = [await make_bid(b).insert() for b in bidders for _ in range(2)]
    await make_tailored(profile)
    calls = []
    for model in (Account, ResumeApplication):
        coll = model.get_motor_collection()
        real = coll.find

        def spy(*args, _name=model.__name__, _real=real, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(coll, "find", spy)
    evidence = await evidence_for(bids, {str(b.id): b for b in bidders})
    assert len(evidence) == 6
    assert sorted(calls) == ["Account", "ResumeApplication"]
