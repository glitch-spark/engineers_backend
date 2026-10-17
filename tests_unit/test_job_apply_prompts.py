"""Job Applies resume prompts (in-memory MongoDB via mongomock-motor): each profile row gets a prompt suggested from
the job (role + skills), the user can choose another one, and tailoring uses it."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.job_apply.schema import JobExtraction, Term
from app.models import Account, ResumeApplication, UserExtra
from app.models.job_apply import JobApplicationLog, JobApplyRow, JobApplyRun, ProfileGates, RunSelection
from app.resume_prompts import apply_prompts, prompts_view
from app.routers.job_applies import (
    PromptChoiceBody,
    _effective_prompt,
    _prompt_ctx,
    _row_summary,
    _run_out,
    choose_prompt,
)

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

DEVOPS_JOB = JobExtraction(title="Senior DevOps Engineer", terms=[Term(term="Kubernetes", tier="required"),
                                                                  Term(term="AWS", tier="core")])


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["job-apply-prompts-unit"]
    await init_beanie(
        database=database,
        document_models=[UserExtra, Account, JobApplyRun, JobApplyRow, ResumeApplication, JobApplicationLog],
    )
    return database


async def _setup(blake_prompts=None):
    """Blake has Default + DevOps prompts (or `blake_prompts`); Sam only his older single prompt. Job A is a DevOps
    job open to both; job B hasn't been read yet."""
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    blake = Account(name="Blake", createdBy=owner.id, styleTemplate="<html>{{resume}}</html>")
    apply_prompts(blake, blake_prompts or [
        {"id": "default", "name": "Default", "role": None, "body": "default text"},
        {"id": "new-1", "name": "DevOps", "role": "devops", "body": "devops text"},
    ], "default")
    await blake.insert()
    sam = await Account(name="Sam", createdBy=owner.id, styleTemplate="<html>{{resume}}</html>",
                        resumePromptBody="sam text").insert()
    run = await JobApplyRun(userId=owner.id, fileName="jobs.csv", status="done", threshold=75,
                            selection=[RunSelection(accountId=a.id) for a in (blake, sam)]).insert()
    gates = [ProfileGates(accountId=str(a.id)) for a in (blake, sam)]
    job_a = await JobApplyRow(runId=run.id, userId=owner.id, rowIndex=0, url="https://jobs.example.com/a",
                              title="Senior DevOps Engineer", company="Acme", status="scored", screen="valid",
                              jdText="We need a DevOps engineer.", extraction=DEVOPS_JOB, profileGates=gates,
                              tailorOnly=[str(blake.id), str(sam.id)]).insert()
    job_b = await JobApplyRow(runId=run.id, userId=owner.id, rowIndex=1, url="https://jobs.example.com/b",
                              status="pending", profileGates=gates).insert()
    return owner, blake, sam, run, job_a, job_b


def _id(acc, name):
    return next(p.id for p in prompts_view(acc)[0] if p.name == name)


async def test_run_profiles_list_their_prompts(db):
    _, blake, sam, run, _, _ = await _setup()

    profiles = {p["name"]: p for p in (await _run_out(run, with_profiles=True))["profiles"]}

    assert [(p["name"], p["role"]) for p in profiles["Blake"]["prompts"]] == [("Default", None), ("DevOps", "devops")]
    assert profiles["Blake"]["defaultPromptId"] == "default"
    assert profiles["Sam"]["prompts"] == [{"id": "default", "name": "Default", "role": None}]


async def test_row_summary_has_job_role_and_suggestion(db):
    _, blake, sam, run, job_a, _ = await _setup()

    out = _row_summary(job_a, run.threshold, prompt_ctx=await _prompt_ctx(run))

    assert out["jobRole"]["role"] == "devops" and out["jobRole"]["marks"] == ["Kubernetes", "AWS"]
    assert out["prompt"][str(blake.id)] == {"suggestedId": _id(blake, "DevOps"), "chosenId": None}
    assert out["prompt"][str(sam.id)] == {"suggestedId": "default", "chosenId": None}


async def test_choose_prompt_saves_and_clearing_by_choosing_the_suggestion(db):
    owner, blake, _, _, job_a, _ = await _setup()

    out = await choose_prompt(str(job_a.id), PromptChoiceBody(accountId=str(blake.id), promptId="default"), owner)
    assert out["prompt"][str(blake.id)]["chosenId"] == "default"
    assert (await JobApplyRow.get(job_a.id)).promptChoice == {str(blake.id): "default"}

    out = await choose_prompt(str(job_a.id), PromptChoiceBody(accountId=str(blake.id), promptId=_id(blake, "DevOps")), owner)
    assert out["prompt"][str(blake.id)]["chosenId"] is None
    assert (await JobApplyRow.get(job_a.id)).promptChoice == {}


async def test_choice_of_a_deleted_prompt_falls_back(db):
    three = [
        {"id": "default", "name": "Default", "role": None, "body": "d"},
        {"id": "new-1", "name": "DevOps", "role": "devops", "body": "v"},
        {"id": "new-2", "name": "Backend", "role": "backend", "body": "b"},
    ]
    owner, blake, _, run, job_a, _ = await _setup(three)
    await choose_prompt(str(job_a.id), PromptChoiceBody(accountId=str(blake.id), promptId=_id(blake, "Backend")), owner)
    kept = [p.model_dump() for p in prompts_view(blake)[0] if p.name != "Backend"]
    apply_prompts(blake, kept, "default")
    await blake.save()

    row = await JobApplyRow.get(job_a.id)
    out = _row_summary(row, run.threshold, prompt_ctx=await _prompt_ctx(run))

    assert out["prompt"][str(blake.id)]["chosenId"] is None
    assert _effective_prompt(row, str(blake.id), *prompts_view(blake)).name == "DevOps"


async def test_job_without_extraction_suggests_the_default(db):
    _, blake, _, run, _, job_b = await _setup()

    out = _row_summary(job_b, run.threshold, prompt_ctx=await _prompt_ctx(run))

    assert out["jobRole"] == {"role": None, "marks": [], "reason": ""}
    assert out["prompt"][str(blake.id)]["suggestedId"] == "default"


async def test_choose_prompt_rejects_unknown_profile_or_prompt(db):
    owner, blake, _, _, job_a, _ = await _setup()

    for body in (PromptChoiceBody(accountId="000000000000000000000000", promptId="default"),
                 PromptChoiceBody(accountId=str(blake.id), promptId="nope")):
        with pytest.raises(HTTPException) as err:
            await choose_prompt(str(job_a.id), body, owner)
        assert err.value.status_code == 404
