"""An assigned platform-user caller opens the interview and its brief files (the linked resume, the profile's resume
files) and nothing else. Real routes over mongomock-motor; only the PDF fetch is patched."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import caller_access
from app.models import Account, ResumeApplication, UserExtra
from app.models.account import Resume
from app.models.interview import Interview, InterviewCaller, InterviewResumeLink, InterviewStageEvent
from app.models.interview_question import InterviewQuestion

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db(monkeypatch):
    from app.routers import accounts, resume

    database = AsyncMongoMockClient()["caller-access-unit"]
    await init_beanie(
        database=database,
        document_models=[Interview, UserExtra, Account, InterviewQuestion, ResumeApplication],
    )

    async def fake_pdf(job):
        return b"%PDF-fake"

    monkeypatch.setattr(resume, "_fetch_pdf_bytes", fake_pdf)
    monkeypatch.setattr(accounts.resume_files, "presigned_url", lambda key, name: f"https://s3.example/{key}")
    return database


@pytest.fixture
def client(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import accounts, interviews, resume

    def make(user):
        app = FastAPI()
        for r in (interviews.router, resume.router, accounts.router):
            app.include_router(r)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


async def make_user(name="Pedro", role="staff", **kw) -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, **kw).insert()


async def make_brief(*, caller=None, enabled=True, kind="user"):
    """Owner, their profile (one stored resume file), a resume job, and an interview linked to both."""
    owner = await make_user(f"Owen{PydanticObjectId()}")
    acc = await Account(name="Alex", createdBy=owner.id).insert()
    acc.resumes = [Resume(filename="cv.pdf", markdown="x", s3Key="k/cv.pdf")]
    await acc.save()
    job = await ResumeApplication(
        userId=owner.id, accountId=acc.id, profileName="Alex", companyName="Acme", companyNameLower="acme",
        jobDescription="jd", status="completed",
    ).insert()
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, endsAt=START.replace(hour=16), status="scheduled",
        caller=InterviewCaller(
            enabled=enabled, callerId=str(caller.id) if caller else None, callerName="Cal", callerKind=kind,
            startsAt=START, method="video",
        ),
    )
    iv = await Interview(
        accountId=acc.id, createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[entry.model_dump()], stage="intro", status="scheduled",
        resumeLink=InterviewResumeLink(kind="tailored", resumeJobId=job.id),
    ).insert()
    return owner, acc, job, iv


async def test_assigned_caller_can_get_interview(client):
    caller, other = await make_user("Cal"), await make_user("Eve")
    _, _, _, iv = await make_brief(caller=caller)
    async with client(caller) as c:
        assert (await c.get(f"/interviews/{iv.id}")).status_code == 200
    async with client(other) as c:  # anyone else signed in gets only the read-only summary
        r = await c.get(f"/interviews/{iv.id}")
        assert r.status_code == 200 and r.json()["summaryOnly"] is True


async def summary_only(c, iv_id) -> bool:
    """The interview opens as the read-only summary and its questions stay closed."""
    r = await c.get(f"/interviews/{iv_id}")
    return (
        r.status_code == 200
        and r.json().get("summaryOnly") is True
        and (await c.get(f"/interviews/{iv_id}/questions")).status_code == 403
    )


async def test_external_or_self_or_inactive_caller_gets_summary_only(client):
    caller = await make_user("Cal")
    _, _, _, iv = await make_brief(caller=caller, kind="external")  # id matches but it's an external caller
    _, _, _, iv_off = await make_brief(caller=caller, enabled=False)  # round is Self
    async with client(caller) as c:
        assert await summary_only(c, iv.id)
        assert await summary_only(c, iv_off.id)
    gone = await make_user("Gus", isActive=False)
    _, _, _, iv_gone = await make_brief(caller=gone)
    async with client(gone) as c:
        assert await summary_only(c, iv_gone.id)


async def test_caller_downloads_linked_resume_only_with_interview_context(client):
    caller = await make_user("Cal")
    owner, acc, job, iv = await make_brief(caller=caller)
    other_job = await ResumeApplication(
        userId=owner.id, accountId=acc.id, profileName="Alex", companyName="Other", companyNameLower="other",
        jobDescription="jd", status="completed",
    ).insert()
    async with client(caller) as c:
        ok = await c.get(f"/resume/jobs/{job.id}/download?format=pdf&interviewId={iv.id}")
        assert ok.status_code == 200 and ok.content == b"%PDF-fake"
        assert (await c.get(f"/resume/jobs/{job.id}/download?format=pdf")).status_code == 404
        assert (await c.get(f"/resume/jobs/{other_job.id}/download?format=pdf&interviewId={iv.id}")).status_code == 404


async def test_bad_interview_context_is_ignored_not_an_error(client):
    caller = await make_user("Cal")
    _, _, job, iv = await make_brief(caller=caller)
    stranger = await make_user("Eve")
    async with client(caller) as c:
        for bad in ("not-an-id", str(PydanticObjectId()), ""):
            r = await c.get(f"/resume/jobs/{job.id}/download?format=pdf&interviewId={bad}")
            assert r.status_code == 404, bad
    async with client(stranger) as c:  # someone else's interview id grants nothing
        assert (await c.get(f"/resume/jobs/{job.id}/download?format=pdf&interviewId={iv.id}")).status_code == 404


async def test_caller_lists_profile_resume_files_via_interview(client):
    caller = await make_user("Cal")
    owner, acc, _, iv = await make_brief(caller=caller)
    other_acc = await Account(name="Else", createdBy=owner.id).insert()
    async with client(caller) as c:
        ok = await c.get(f"/accounts/{acc.id}/resume-files?interviewId={iv.id}")
        assert ok.status_code == 200 and [r["filename"] for r in ok.json()["resumes"]] == ["cv.pdf"]
        rid = ok.json()["resumes"][0]["id"]
        f = await c.get(f"/accounts/{acc.id}/resumes/{rid}/file?interviewId={iv.id}")
        assert f.status_code == 200 and f.json()["url"] == "https://s3.example/k/cv.pdf"
        assert (await c.get(f"/accounts/{acc.id}/resume-files")).status_code == 404
        assert (await c.get(f"/accounts/{acc.id}/resumes/{rid}/file")).status_code == 404
        assert (await c.get(f"/accounts/{other_acc.id}/resume-files?interviewId={iv.id}")).status_code == 404
        assert (await c.get(f"/accounts/{acc.id}/resume-files?interviewId=zzz")).status_code == 404


async def test_caller_cannot_restore(client):
    caller = await make_user("Cal")
    _, _, job, iv = await make_brief(caller=caller)
    async with client(caller) as c:
        assert (await c.post(f"/resume/jobs/{job.id}/restore-pdf")).status_code == 404
        assert (await c.post(f"/resume/jobs/{job.id}/restore-pdf?interviewId={iv.id}")).status_code == 404


async def test_caller_interview_helper(db):
    caller, other = await make_user("Cal"), await make_user("Eve")
    _, _, _, iv = await make_brief(caller=caller)
    got = await caller_access.caller_interview(str(iv.id), caller)
    assert got is not None and got.id == iv.id
    assert await caller_access.caller_interview(str(iv.id), other) is None
    assert await caller_access.caller_interview(None, caller) is None
    assert await caller_access.caller_interview("nope", caller) is None
