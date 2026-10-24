"""Resume Generator resumes scored against their pasted JD, the way Job Applies scores tailored ones (in-memory
MongoDB via mongomock-motor; the LLM extraction is faked)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from types import SimpleNamespace

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.job_apply import extract, tailored_score
from app.job_apply.schema import JobExtraction, MinYears, Term
from app.models import UserExtra
from app.models.job_apply import JobApplyRow, JobApplyRun, JobExtractionCache

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

JD = (
    "Senior Python Developer. We need 5+ years building APIs with Python, FastAPI and PostgreSQL. "
    "Kafka is a plus. You will design services and mentor engineers."
)
EX = JobExtraction(
    title="Senior Python Developer",
    seniority="senior",
    minYears=MinYears(value=5, required=True),
    terms=[
        Term(term="Python", tier="required"),
        Term(term="FastAPI", tier="required"),
        Term(term="PostgreSQL", tier="core"),
        Term(term="Kafka", tier="preferred"),
    ],
)
RESUME = """Jane Doe
Senior Python Developer
EXPERIENCE
Senior Python Developer
Acme
Jan 2019 – Present
Built FastAPI services on PostgreSQL in Python; mentored engineers.
SKILLS
Python, FastAPI, PostgreSQL
"""


@pytest.fixture
async def llm_calls(monkeypatch):
    database = AsyncMongoMockClient()["manual-score-unit"]
    await init_beanie(database=database, document_models=[UserExtra, JobApplyRun, JobApplyRow, JobExtractionCache])
    llm = SimpleNamespace(jds=[], ex=EX)  # the JDs sent to the LLM, and what it answers

    async def fake_extract(jd, *, user, run_date, **kw):
        llm.jds.append(jd)
        return llm.ex, "llm"

    monkeypatch.setattr(extract, "extract_job", fake_extract)
    return llm


def _job(jd=JD, text=RESUME, source="generator"):
    return SimpleNamespace(id="j1", source=source, jobDescription=jd, plainText=text, jobApplyRowId=None)


async def test_scores_a_generator_resume_against_its_pasted_jd(llm_calls):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()

    m = await tailored_score.score_job(_job(), owner)

    assert m is not None and 0 < m.total <= 100
    assert m.missing == ["Kafka"] and m.knockouts == []
    assert llm_calls.jds == [JD]


async def test_the_same_jd_is_read_once(llm_calls):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()

    first = await tailored_score.score_job(_job(), owner)
    second = await tailored_score.score_job(_job(), owner)

    assert first == second and llm_calls.jds == [JD]  # the second came from JobExtractionCache


async def test_weights_come_from_the_users_latest_run(llm_calls):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    run = await JobApplyRun(userId=owner.id, fileName="jobs.csv", status="done", threshold=75).insert()
    await JobApplyRow(runId=run.id, userId=owner.id, rowIndex=0, url="https://x.io/a", jdText=JD, extraction=EX).insert()
    await JobApplyRow(runId=run.id, userId=owner.id, rowIndex=1, url="https://x.io/b", jdText="Java developer with Spring",
                      extraction=EX).insert()

    with_run = await tailored_score.score_job(_job(), owner)
    without_run = await tailored_score.score_job(_job(), None)

    assert with_run is not None and without_run is not None
    assert with_run.missing == without_run.missing  # same terms; only the relevance weights differ


async def test_no_score_without_a_jd_text_or_a_job_posting(llm_calls):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()

    assert await tailored_score.score_job(_job(jd="  "), owner) is None
    assert await tailored_score.score_job(_job(text=""), owner) is None
    assert llm_calls.jds == []

    llm_calls.ex = EX.model_copy(update={"isJobPosting": False})
    assert await tailored_score.score_job(_job(jd="Page not found"), owner) is None


async def test_a_failing_extraction_never_fails_the_job(llm_calls, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(JobExtractionCache, "find_one", boom)

    assert await tailored_score.score_job(_job(), None) is None
