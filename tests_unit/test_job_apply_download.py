"""Downloading resumes from Tailor & apply (in-memory MongoDB via mongomock-motor): one file per job x profile (the
tailored resume when ready, else the highest template resume at or above the minimum score), saved as
<Profile>/<Company>/Resume.pdf like the Resume page."""

import io
import os
import zipfile

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app import resume_files
from app.job_apply.schema import ResumeScore
from app.models import Account, ResumeApplication, UserExtra
from app.models.account import Resume
from app.models.job_apply import JobApplicationLog, JobApplyRow, JobApplyRun, ProfileGates, RunSelection
from app.routers import job_applies
from app.routers.job_applies import DownloadBody, download_files, download_zip, row_resume_file

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["download-unit"]  # fresh in-memory database per test
    await init_beanie(
        database=database,
        document_models=[UserExtra, Account, JobApplyRun, JobApplyRow, ResumeApplication, JobApplicationLog],
    )

    async def fake_template(key):
        return f"template:{key}".encode()

    async def fake_tailored(job):
        return f"tailored:{job.id}".encode()

    monkeypatch.setattr(resume_files, "read_resume_file", fake_template)
    monkeypatch.setattr(job_applies, "fetch_tailored_pdf", fake_tailored)
    return database


def _score(acc, resume: Resume, total: int) -> ResumeScore:
    return ResumeScore(accountId=str(acc.id), resumeId=str(resume.id), filename=resume.filename, total=total, band="good")


async def _setup():
    """Minimum score 75. Job A at Acme: Alex's templates score 82 and 78, Sam's 61, Jordan has no template but a
    finished tailored resume. Job B at Globex: only Sam, at 60."""
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    alex_cv, alex_cv2 = Resume(filename="alex_backend.pdf", markdown="x", s3Key="k/a1.pdf"), Resume(
        filename="alex_frontend.pdf", markdown="x", s3Key="k/a2.pdf")
    sam_cv = Resume(filename="sam.pdf", markdown="x", s3Key="k/s1.pdf")
    alex = await Account(name="Alex", createdBy=owner.id, resumes=[alex_cv, alex_cv2]).insert()
    sam = await Account(name="Sam", createdBy=owner.id, resumes=[sam_cv]).insert()
    jordan = await Account(name="Jordan", createdBy=owner.id, styleTemplate="<html/>").insert()
    run = await JobApplyRun(
        userId=owner.id, fileName="jobs.csv", status="done", threshold=75,
        selection=[RunSelection(accountId=a.id) for a in (alex, sam, jordan)],
    ).insert()
    tailored = await ResumeApplication(
        userId=owner.id, accountId=jordan.id, profileName="Jordan", companyName="Acme", companyNameLower="acme",
        jobDescription="jd", status="completed", source="job_applies", countsAsBid=False,
    ).insert()
    job_a = await JobApplyRow(
        runId=run.id, userId=owner.id, rowIndex=0, url="https://jobs.example.com/a", company="Acme", status="scored",
        screen="valid", topScore=82, tailorOnly=[str(jordan.id)], tailored={str(jordan.id): tailored.id},
        bestByProfile=[_score(alex, alex_cv, 82), _score(sam, sam_cv, 61)],
        scores=[_score(alex, alex_cv2, 78), _score(alex, alex_cv, 82), _score(sam, sam_cv, 61)],
        profileGates=[ProfileGates(accountId=str(a.id)) for a in (alex, sam, jordan)],
    ).insert()
    job_b = await JobApplyRow(
        runId=run.id, userId=owner.id, rowIndex=1, url="https://jobs.example.com/b", company="Globex", status="scored",
        screen="valid", topScore=60, bestByProfile=[_score(sam, sam_cv, 60)], scores=[_score(sam, sam_cv, 60)],
        profileGates=[ProfileGates(accountId=str(sam.id))],
    ).insert()
    return owner, (alex, sam, jordan), (alex_cv, alex_cv2, sam_cv), run, job_a, job_b, tailored


def _paths(files):
    return [f"{f['folder']}/{f['filename']}" for f in files]


async def test_one_file_per_job_and_profile_with_the_highest_template(db):
    owner, (alex, _, _), (alex_cv, _, _), run, job_a, job_b, tailored = await _setup()

    out = await download_files(str(run.id), DownloadBody(rowIds=[str(job_a.id), str(job_b.id)]), owner)

    assert _paths(out["files"]) == ["Alex/Acme/Resume.pdf", "Jordan/Acme/Resume.pdf"]
    alex_file, jordan_file = out["files"]
    assert (alex_file["kind"], alex_file["resumeId"]) == ("uploaded", str(alex_cv.id))  # 82, not the 78 one
    assert (jordan_file["kind"], jordan_file["tailoredJobId"]) == ("tailored", str(tailored.id))
    assert out["skipped"] == 2  # Sam on both jobs: below the minimum and nothing tailored yet


async def test_the_profile_filter_keeps_only_that_profile(db):
    owner, (alex, _, _), _, run, job_a, job_b, _ = await _setup()

    out = await download_files(str(run.id), DownloadBody(rowIds=[str(job_a.id)], accountId=str(alex.id)), owner)

    assert _paths(out["files"]) == ["Alex/Acme/Resume.pdf"] and out["skipped"] == 0


async def test_two_jobs_at_one_company_get_their_own_folders(db):
    owner, _, _, run, job_a, job_b, _ = await _setup()
    job_b.company = "Acme"
    job_b.bestByProfile, job_b.scores = job_a.bestByProfile, job_a.scores
    job_b.profileGates = job_a.profileGates
    await job_b.save()

    out = await download_files(str(run.id), DownloadBody(rowIds=[str(job_a.id), str(job_b.id)]), owner)

    assert _paths(out["files"]) == ["Alex/Acme/Resume.pdf", "Jordan/Acme/Resume.pdf", "Alex/Acme (2)/Resume.pdf"]


async def test_single_files_resolve_to_the_same_folders(db):
    owner, (alex, _, _), (_, alex_cv2, _), run, job_a, _, tailored = await _setup()
    body = DownloadBody(files=[
        {"kind": "uploaded", "rowId": str(job_a.id), "accountId": str(alex.id), "resumeId": str(alex_cv2.id)},
        {"kind": "tailored", "tailoredJobId": str(tailored.id)},
    ])

    out = await download_files(str(run.id), body, owner)

    assert _paths(out["files"]) == ["Alex/Acme/Resume.pdf", "Jordan/Acme/Resume.pdf"]
    assert out["files"][0]["resumeId"] == str(alex_cv2.id)  # the one clicked, even if not the best


async def test_a_file_from_outside_the_run_is_refused(db):
    owner, (alex, _, _), _, run, _, _, _ = await _setup()
    stranger = await JobApplyRow(runId=run.id, userId=owner.id, rowIndex=9, company="Initech").insert()
    body = DownloadBody(files=[{"kind": "uploaded", "rowId": str(stranger.id), "accountId": str(alex.id),
                                "resumeId": "000000000000000000000000"}])

    with pytest.raises(HTTPException) as err:
        await download_files(str(run.id), body, owner)

    assert err.value.status_code == 404


async def test_template_file_comes_with_its_folder(db):
    owner, (alex, _, _), (alex_cv, _, _), _, job_a, _, _ = await _setup()

    res = await row_resume_file(str(job_a.id), str(alex.id), str(alex_cv.id), owner)

    assert res.body == b"template:k/a1.pdf"
    assert res.headers["X-Folder"] == "Alex/Acme" and res.media_type == "application/pdf"


async def test_zip_has_the_same_folders(db):
    owner, _, _, run, job_a, job_b, tailored = await _setup()

    res = await download_zip(str(run.id), DownloadBody(rowIds=[str(job_a.id), str(job_b.id)]), owner)

    with zipfile.ZipFile(io.BytesIO(res.body)) as zf:
        assert zf.namelist() == ["Alex/Acme/Resume.pdf", "Jordan/Acme/Resume.pdf"]
        assert zf.read("Jordan/Acme/Resume.pdf") == f"tailored:{tailored.id}".encode()
        assert zf.read("Alex/Acme/Resume.pdf") == b"template:k/a1.pdf"


# ---------- status counts: template vs tailored, and what still needs tailoring ----------


async def _tailoring_for_sam(owner, sam, job_b):
    """Sam's tailored resume for job B is still being written."""
    queued = await ResumeApplication(
        userId=owner.id, accountId=sam.id, profileName="Sam", companyName="Globex", companyNameLower="globex",
        jobDescription="jd", status="queued", source="job_applies", countsAsBid=False,
    ).insert()
    job_b.tailored = {str(sam.id): queued.id}
    await job_b.save()


async def test_run_counts_split_ready_and_waiting(db):
    owner, (_, sam, _), _, run, _, job_b, _ = await _setup()
    await _tailoring_for_sam(owner, sam, job_b)

    counts = await job_applies._application_counts(run)

    # Alex sends his template, Jordan his tailored resume; Sam on A should tailor, Sam on B is tailoring
    assert (counts["readyTemplate"], counts["readyTailored"]) == (1, 1)
    assert (counts["needsResume"], counts["tailoring"]) == (1, 1)


async def test_export_preview_splits_rows_and_waiting(db, monkeypatch):
    owner, (_, sam, _), _, run, _, job_b, _ = await _setup()
    await _tailoring_for_sam(owner, sam, job_b)

    async def sheet_info(http, sheet_id):
        return {"title": "Team sheet", "tabs": {}}

    monkeypatch.setattr(job_applies.google_sheets, "sheet_info", sheet_info)
    body = job_applies.ExportBody(sheetUrl="https://docs.google.com/spreadsheets/d/abcdefghijklmnopqrstuvwxyz/edit", dryRun=True)

    out = await job_applies.export_sheet(str(run.id), body, owner)

    assert (out["ready"], out["readyTemplate"], out["readyTailored"]) == (2, 1, 1)
    assert (out["waiting"], out["needsTailoring"], out["tailoring"]) == (2, 1, 1)
