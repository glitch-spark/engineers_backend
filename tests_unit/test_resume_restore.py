"""Making a resume job's missing PDF again (app/resume_restore.py) and the download's 410 for a missing file
(in-memory MongoDB via mongomock-motor; storage and the model faked)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timezone

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app import resume_restore, resume_service
from app.job_apply import fixed_forks
from app.models import Account, ResumeApplication, UserExtra
from app.models.account import Resume
from app.resume_service import GeneratedResume
from app.routers import resume as resume_router

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def env(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["resume-restore-unit"],
                      document_models=[UserExtra, Account, ResumeApplication])
    calls = {"generate": [], "copy": [], "present": set()}

    async def fake_exists(key):
        return key in calls["present"]

    async def fake_generate(account, jd, company, **kw):
        calls["generate"].append({"jd": jd, "company": company, **kw})
        return GeneratedResume(pdf_bytes=b"%PDF-new", s3_key="2026_11_06/new/Resume.pdf", plain_text="new text",
                               cost_usd=0.02)

    async def fake_copy(src, dst):
        calls["copy"].append((src, dst))
        return True

    monkeypatch.setattr(resume_service, "s3_enabled", lambda: True)
    monkeypatch.setattr(resume_restore, "file_exists", fake_exists)
    monkeypatch.setattr(resume_service, "generate_resume", fake_generate)
    monkeypatch.setattr(fixed_forks, "copy_file", fake_copy)
    return calls


async def _job(**kw):
    owner = await UserExtra(name="Ana", email="ana@x.io").insert()
    acc = await Account(name="Jordan", createdBy=owner.id, styleTemplate="<html><body>x</body></html>").insert()
    fields = dict(
        userId=owner.id, accountId=acc.id, profileName="Jordan", companyName="Hike2", companyNameLower="hike2",
        jobDescription="Python role", status="completed", s3Key="2026_11_02/old/Resume.pdf", plainText="old text",
        promptOverride="Tailor it", coverLetterText="Dear Hike2", matchScore={"total": 80, "band": "strong"},
        estimatedCostUsd=0.05, pdfFilename="Resume.pdf",
    )
    fields.update(kw)
    return owner, acc, await ResumeApplication(**fields).insert()


async def test_regenerates_only_the_pdf_with_the_stored_jd_and_prompt(env):
    _owner, _acc, job = await _job(tailorJobDescription="Trimmed Python role", resumeModelId="openai:gpt-4o-mini")
    out = await resume_restore.restore_pdf(job)
    assert out["kind"] == "regenerated"
    (call,) = env["generate"]
    assert call["jd"] == "Trimmed Python role" and call["prompt_body"] == "Tailor it"
    assert call["model_id"] == "openai:gpt-4o-mini" and call["job_id"] == str(job.id)
    saved = await ResumeApplication.get(job.id)
    assert (saved.s3Key, saved.plainText) == ("2026_11_06/new/Resume.pdf", "new text")
    assert saved.coverLetterText == "Dear Hike2" and saved.matchScore.total == 80  # untouched
    assert saved.estimatedCostUsd == pytest.approx(0.07)
    assert saved.pdfRegeneratedAt is not None and saved.pdfRestoreLeaseAt is None


async def test_nothing_to_do_when_the_file_is_there(env):
    _owner, _acc, job = await _job()
    env["present"].add(job.s3Key)
    with pytest.raises(resume_restore.RestoreError) as e:
        await resume_restore.restore_pdf(job)
    assert e.value.status == 409 and not env["generate"]


async def test_no_jd_left_after_retention(env):
    _owner, _acc, job = await _job(jobDescription="", tailorJobDescription=None)
    with pytest.raises(resume_restore.RestoreError) as e:
        await resume_restore.restore_pdf(job)
    assert e.value.status == 422 and "30 days" in e.value.message
    assert (await ResumeApplication.get(job.id)).pdfRestoreLeaseAt is None  # the lease is let go


async def test_only_completed_jobs_and_one_restore_at_a_time(env):
    _owner, _acc, queued = await _job(status="queued")
    with pytest.raises(resume_restore.RestoreError) as e:
        await resume_restore.restore_pdf(queued)
    assert e.value.status == 409
    busy = await ResumeApplication(**{**queued.model_dump(exclude={"id"}), "status": "completed",
                                      "pdfRestoreLeaseAt": datetime.now(timezone.utc)}).insert()
    with pytest.raises(resume_restore.RestoreError) as e:
        await resume_restore.restore_pdf(busy)
    assert e.value.status == 409 and "already" in e.value.message and not env["generate"]


async def test_fixed_fork_is_copied_again_from_the_profile_file(env):
    owner, acc, _ = await _job()
    acc.resumes = [Resume(filename="ERRIC.pdf", markdown="x", s3Key="profiles/a/resumes/r.pdf")]
    await acc.save()
    fork = await ResumeApplication(
        userId=owner.id, accountId=acc.id, companyName="Voxology", companyNameLower="voxology", jobDescription="jd",
        status="completed", s3Key="job-applies/fixed/a/f/ERRIC.pdf", fixedFrom=str(acc.resumes[0].id),
    ).insert()
    with pytest.raises(resume_restore.RestoreError) as e:  # the profile's own file is gone too
        await resume_restore.restore_pdf(fork)
    assert e.value.status == 422 and "ERRIC.pdf" in e.value.message
    env["present"].add("profiles/a/resumes/r.pdf")
    out = await resume_restore.restore_pdf(fork)
    assert out["kind"] == "copied" and env["copy"] == [("profiles/a/resumes/r.pdf", "job-applies/fixed/a/f/ERRIC.pdf")]
    assert not env["generate"]


async def test_endpoint_access_follows_the_download(env):
    owner, _acc, job = await _job()
    manager = await UserExtra(name="Puma", email="puma@x.io", role="interview_manager").insert()
    other = await UserExtra(name="Ben", email="ben@x.io").insert()
    with pytest.raises(HTTPException) as e:
        await resume_router.restore_job_pdf(str(job.id), other)
    assert e.value.status_code == 404
    assert (await resume_router.restore_job_pdf(str(job.id), manager))["kind"] == "regenerated"
    env["present"].add("2026_11_06/new/Resume.pdf")
    with pytest.raises(HTTPException) as e:  # done: the file is there now
        await resume_router.restore_job_pdf(str(job.id), owner)
    assert e.value.status_code == 409


async def test_download_answers_410_for_a_missing_file(env, monkeypatch):
    from botocore.exceptions import ClientError

    owner, _acc, job = await _job()

    class Missing:
        def get_object(self, **kw):
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Key not found"}}, "GetObject")

    monkeypatch.setattr(resume_service, "_s3_client", lambda: Missing())
    with pytest.raises(HTTPException) as e:
        await resume_router.download_job(str(job.id), owner, format="pdf")
    assert e.value.status_code == 410
    # Chromium keeps a 410 with no cache headers forever: once the file is made again, the browser would still
    # answer "gone" from its cache without asking the server.
    assert e.value.headers["Cache-Control"] == "no-store"


async def test_a_downloaded_pdf_is_not_cached_either(env, monkeypatch):
    owner, _acc, job = await _job()

    class Stored:
        def get_object(self, **kw):
            return {"Body": type("B", (), {"read": lambda self: b"%PDF-1"})()}

    monkeypatch.setattr(resume_service, "_s3_client", lambda: Stored())
    res = await resume_router.download_job(str(job.id), owner, format="pdf")
    assert res.body == b"%PDF-1" and res.headers["Cache-Control"] == "no-store"
