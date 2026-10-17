"""Tailor & apply against an in-memory MongoDB (mongomock-motor): the run's minimum score decides which applications
can go with their uploaded resume and which need a tailored one. It no longer hides jobs or uploaded scores."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.job_apply.schema import ResumeScore
from app.models import Account, ResumeApplication, UserExtra
from app.models.job_apply import JobApplicationLog, JobApplyRow, JobApplyRun, ProfileGates, RunSelection
from app.routers.job_applies import (
    TailorAllBody,
    _app_context,
    _application_counts,
    _applications,
    _row_summary,
    _rows_query,
    _run_out,
    _suggested_count,
    tailor_all,
)

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["min-score-unit"]  # fresh in-memory database per test
    await init_beanie(
        database=database,
        document_models=[UserExtra, Account, JobApplyRun, JobApplyRow, ResumeApplication, JobApplicationLog],
    )
    return database


def _score(acc, total: int, name: str = "") -> ResumeScore:
    band = "good" if total >= 75 else "weak"
    name = name or acc.name
    return ResumeScore(accountId=str(acc.id), resumeId=f"res-{name}", filename=f"{name}.pdf", total=total, band=band)


async def _setup():
    """Minimum score 75. Job A: Alex 82 (his second resume 70), Sam 61, Jordan has no uploaded resume. Job B: only
    Sam, at 60."""
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    alex, sam, jordan = [
        await Account(name=n, createdBy=owner.id, styleTemplate="<html>{{resume}}</html>").insert()
        for n in ("Alex", "Sam", "Jordan")
    ]
    run = await JobApplyRun(
        userId=owner.id, fileName="jobs.csv", status="done", threshold=75,
        selection=[RunSelection(accountId=a.id) for a in (alex, sam, jordan)],
    ).insert()
    job_a = await JobApplyRow(
        runId=run.id, userId=owner.id, rowIndex=0, url="https://jobs.example.com/a", company="Acme", status="scored",
        screen="valid", jdText="We need a backend engineer.", topScore=82,
        bestByProfile=[_score(alex, 82), _score(sam, 61)], tailorOnly=[str(jordan.id)],
        scores=[_score(sam, 61), _score(alex, 70, "Alex-frontend"), _score(alex, 82)],
        profileGates=[ProfileGates(accountId=str(a.id)) for a in (alex, sam, jordan)],
    ).insert()
    job_b = await JobApplyRow(
        runId=run.id, userId=owner.id, rowIndex=1, url="https://jobs.example.com/b", company="Globex", status="scored",
        screen="valid", jdText="We need a frontend engineer.", topScore=60,
        bestByProfile=[_score(sam, 60)], profileGates=[ProfileGates(accountId=str(sam.id))],
    ).insert()
    return owner, (alex, sam, jordan), run, job_a, job_b


async def test_suggested_lists_jobs_whose_scores_are_all_below_the_minimum(db):
    _, _, run, job_a, job_b = await _setup()

    rows = await JobApplyRow.find(await _rows_query(run, view="suggested")).sort("+rowIndex").to_list()

    assert [r.id for r in rows] == [job_a.id, job_b.id]
    assert await _suggested_count(run) == 2


async def test_a_job_shows_every_uploaded_score_even_below_the_minimum(db):
    _, (alex, sam, _), run, job_a, _ = await _setup()

    out = _row_summary(job_a, run.threshold)

    assert [(s["accountId"], s["total"]) for s in out["suggestions"]] == [(str(alex.id), 82), (str(sam.id), 61)]


async def test_a_job_shows_every_uploaded_resume_of_a_profile_best_first(db):
    _, (alex, sam, _), run, job_a, _ = await _setup()

    out = _row_summary(job_a, run.threshold)

    by_profile = {}
    for r in out["resumeScores"]:
        by_profile.setdefault(r["accountId"], []).append((r["filename"], r["total"], r["band"]))
    assert by_profile == {
        str(alex.id): [("Alex.pdf", 82, "strong"), ("Alex-frontend.pdf", 70, "good")],
        str(sam.id): [("Sam.pdf", 61, "fair")],
    }
    assert [r["accountId"] for r in _row_summary(job_a, 75, account_id=str(sam.id))["resumeScores"]] == [str(sam.id)]


async def test_a_profile_below_the_minimum_needs_a_resume(db):
    _, (alex, sam, jordan), run, job_a, _ = await _setup()
    ctx = await _app_context(run, [job_a])

    apps = {a["accountId"]: a for a in _applications(job_a, run.threshold, ctx)}

    assert apps[str(alex.id)]["state"] == "ready" and apps[str(alex.id)]["resumeId"] == "res-Alex"
    assert (apps[str(sam.id)]["state"], apps[str(sam.id)]["score"]) == ("needsResume", 61)
    assert apps[str(jordan.id)]["state"] == "needsResume"


async def test_counts_split_applications_by_the_minimum_score(db):
    _, _, run, _, _ = await _setup()

    counts = await _application_counts(run)

    # Alex on A can go as is; Sam on A, Jordan on A and Sam on B need tailoring
    assert (counts["noTailorNeeded"], counts["tailorNeeded"]) == (1, 3)
    assert (counts["ready"], counts["needsResume"]) == (1, 3)


async def test_a_lower_minimum_moves_applications_to_no_tailor_needed(db):
    _, _, run, _, _ = await _setup()
    run.threshold = 60
    await run.save()

    counts = await _application_counts(run)

    assert (counts["noTailorNeeded"], counts["tailorNeeded"]) == (3, 1)  # only Jordan (no uploaded resume) is left


@pytest.mark.parametrize("dry_run", [True, False])
async def test_tailor_resumes_only_tailors_applications_below_the_minimum(db, dry_run):
    owner, (alex, sam, jordan), run, job_a, job_b = await _setup()

    result = await tailor_all(str(run.id), TailorAllBody(dryRun=dry_run), owner)

    assert result == {"queued": 3, "skippedCap": 0, "skipped": 0}
    if not dry_run:
        assert set((await JobApplyRow.get(job_a.id)).tailored) == {str(sam.id), str(jordan.id)}  # not Alex (82)
        assert set((await JobApplyRow.get(job_b.id)).tailored) == {str(sam.id)}


async def test_tailor_resumes_for_ticked_jobs_follows_the_same_rule(db):
    owner, (_, sam, jordan), run, job_a, _ = await _setup()

    await tailor_all(str(run.id), TailorAllBody(rowIds=[str(job_a.id)]), owner)

    assert set((await JobApplyRow.get(job_a.id)).tailored) == {str(sam.id), str(jordan.id)}


async def test_run_profiles_say_which_html_template_each_uses(db):
    _, (alex, sam, jordan), run, _, _ = await _setup()
    alex.styleTemplateName = "Modern two-column.html"
    await alex.save()
    sam.styleTemplate = None
    await sam.save()

    out = await _run_out(run, with_profiles=True)

    by_name = {p["name"]: (p["hasTemplate"], p["templateName"]) for p in out["profiles"]}
    # Jordan has a template saved without a file name: it still counts
    assert by_name == {"Alex": (True, "Modern two-column.html"), "Sam": (False, None), "Jordan": (True, "HTML template")}
