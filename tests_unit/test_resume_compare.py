"""Statistics → Resume compare (in-memory MongoDB via mongomock-motor): the jobs with an interview in the period, with
every user's resume for the same job link and its score."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.job_apply import file_links
from app.models import Account, Interview, InterviewQuestion, ResumeApplication, UserExtra
from app.routers.metrics import resume_compare as endpoint

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

URL = "https://jobs.example.com/acme/123"
IN_OCT = datetime(2026, 10, 20, 15)
SCORE = {"total": 84, "band": "strong", "knockouts": [], "missing": ["Kafka"]}


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["resume-compare-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Account, Interview, InterviewQuestion, ResumeApplication])


async def _resume(user, acc, url=URL, when=datetime(2026, 10, 1), **kw):
    return await ResumeApplication(
        userId=user.id, accountId=acc.id, profileName=acc.name, companyName="Acme", companyNameLower="acme",
        jobDescription="jd", jobUrl=url, status="completed", source="job_applies", createdAt=when, **kw,
    ).insert()


async def _interview(user, acc, job_url=URL, when=IN_OCT, company="Acme", **kw):
    return await Interview(
        accountId=acc.id, createdBy=user.id, scheduledAt=when, companyName=company, jobUrl=job_url,
        stageHistory=[{"stage": "intro", "at": when, "scheduledAt": when, "status": "scheduled"}], **kw,
    ).insert()


async def _setup():
    ana, ben, cy = [await UserExtra(name=n, email=f"{n}@x.io").insert() for n in ("Ana", "Ben", "Cy")]
    accs = {u.name: await Account(name=f"{u.name} US", createdBy=u.id).insert() for u in (ana, ben, cy)}
    return (ana, ben, cy), accs


async def test_one_row_per_interviewed_job_with_everyones_resume_and_score(db):
    (ana, ben, cy), accs = await _setup()
    winner = await _resume(ana, accs["Ana"], url=URL + "?utm_source=x", matchScore=SCORE)  # same job, other format
    older = await _resume(ben, accs["Ben"], when=datetime(2026, 9, 1))
    newer = await _resume(ben, accs["Ben"], fixedFrom="res1", matchScore={**SCORE, "total": 71, "band": "good"})
    await _resume(cy, accs["Cy"], url="https://jobs.example.com/other")  # another job, no interview
    await _interview(ana, accs["Ana"])

    out = await endpoint(cy, period="month", date="2026-10-21", q="", region="all")

    assert [u["name"] for u in out["users"]] == ["Cy", "Ana", "Ben"]  # the viewer first
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert (row["company"], row["interviews"][0]["name"], row["interviews"][0]["stage"]) == ("Acme", "Ana", "intro")
    a, = row["cells"][str(ana.id)]
    assert (a["resumeJobId"], a["kind"], a["score"]["total"], a["interviewed"]) == (str(winner.id), "tailored", 84, True)
    assert file_links.read_token(a["link"].rsplit("/", 1)[1])["id"] == str(winner.id)  # a signed link anyone can open
    assert a["link"].startswith("/job-applies/files/")  # the app prefixes the API it talks to, not APP_BASE_URL
    b, = row["cells"][str(ben.id)]
    assert (b["resumeJobId"], b["kind"], b["score"]["total"], b["interviewed"]) == (str(newer.id), "fixed", 71, False)
    assert str(older.id) != b["resumeJobId"] and str(cy.id) not in row["cells"]


async def test_the_interviews_linked_resume_wins_and_its_link_is_used_without_a_job_url(db):
    (ana, ben, _), accs = await _setup()
    linked = await _resume(ana, accs["Ana"], when=datetime(2026, 9, 1))
    await _resume(ana, accs["Ana"], when=datetime(2026, 10, 2))  # newer, but not the one the interview came from
    await _interview(ana, accs["Ana"], job_url=None, resumeLink={"kind": "tailored", "resumeJobId": linked.id})

    out = await endpoint(ben, period="month", date="2026-10-21", q="", region="all")

    a, = out["rows"][0]["cells"][str(ana.id)]
    assert a["resumeJobId"] == str(linked.id) and a["interviewed"]


async def test_an_interview_without_a_resume_still_shows_and_search_and_period_filter(db):
    (ana, ben, _), accs = await _setup()
    await _interview(ben, accs["Ben"], company="Globex", job_url="https://globex.io/jobs/9")
    await _interview(ana, accs["Ana"], when=datetime(2026, 9, 20))  # September: not in October

    out = await endpoint(ana, period="month", date="2026-10-21", q="", region="all")
    assert [r["company"] for r in out["rows"]] == ["Globex"]
    b, = out["rows"][0]["cells"][str(ben.id)]
    assert (b["resumeJobId"], b["interviewed"]) == (None, True)

    assert (await endpoint(ana, period="month", date="2026-10-21", q="acme", region="all"))["rows"] == []


async def test_backfill_keys_older_resumes(db):
    from scripts.backfill_job_url_keys import backfill

    (ana, _, _), accs = await _setup()
    r = await _resume(ana, accs["Ana"])
    await ResumeApplication.get_motor_collection().update_one({"_id": r.id}, {"$unset": {"jobUrlKey": ""}})

    assert await backfill() == {"resumes": 1, "written": 0}
    assert await backfill(apply=True) == {"resumes": 1, "written": 1}
    assert (await ResumeApplication.get(r.id)).jobUrlKey == "jobs.example.com/acme/123"
    assert (await backfill(apply=True))["resumes"] == 0


async def test_resumes_made_before_the_key_are_still_found(db):
    (ana, ben, _), accs = await _setup()
    old = await _resume(ben, accs["Ben"], url="http://www.jobs.example.com/acme/123/?utm_campaign=y", matchScore=SCORE)
    await ResumeApplication.get_motor_collection().update_one({"_id": old.id}, {"$unset": {"jobUrlKey": ""}})
    await _resume(ben, accs["Ben"], url="https://jobs.example.com/acme/999")  # same site, another job
    await _interview(ana, accs["Ana"])

    out = await endpoint(ana, period="month", date="2026-10-21", q="", region="all")

    b, = out["rows"][0]["cells"][str(ben.id)]
    assert (b["resumeJobId"], b["score"]["total"]) == (str(old.id), 84)


async def test_an_old_numeric_score_loads_and_shows_with_its_band(db):
    (ana, ben, _), accs = await _setup()
    r = await _resume(ben, accs["Ben"])
    await ResumeApplication.get_motor_collection().update_one({"_id": r.id}, {"$set": {"matchScore": 69.2}})
    await _interview(ana, accs["Ana"])

    assert (await ResumeApplication.get(r.id)).matchScore.model_dump() == {
        "total": 69, "band": "good", "knockouts": [], "missing": []}
    b, = (await endpoint(ana, period="month", date="2026-10-21", q="", region="all"))["rows"][0]["cells"][str(ben.id)]
    assert b["score"] == {"total": 69, "band": "good", "missing": []}


async def test_a_region_keeps_its_profiles_interviews_and_resumes(db):
    (ana, ben, _), accs = await _setup()
    us = await Account(name="Ana US", createdBy=ana.id, region="US").insert()
    eu = await Account(name="Ben EU", createdBy=ben.id, region="EU").insert()
    await _resume(ana, us, matchScore=SCORE)
    await _resume(ben, eu, matchScore=SCORE)
    await _interview(ana, us)

    assert (await endpoint(ana, period="month", date="2026-10-21", q="", region="EU"))["rows"] == []
    out = await endpoint(ana, period="month", date="2026-10-21", q="", region="US")
    assert list(out["rows"][0]["cells"]) == [str(ana.id)] and out["region"] == "US"
    out = await endpoint(ana, period="month", date="2026-10-21", q="", region="all")
    assert set(out["rows"][0]["cells"]) == {str(ana.id), str(ben.id)}
