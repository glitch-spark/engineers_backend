"""Any signed-in user can open any interview and gets a read-only summary; owner/admin/manager/caller keep the full view.
Real routes over mongomock-motor."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timezone

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
SECRET_WORDS = ("TOPSECRET-NOTE", "TOPSECRET-TRANSCRIPT", "TOPSECRET-LINK", "TOPSECRET-URL", "TOPSECRET-INTERVIEWER")


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["interview-summary-unit"]
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


async def make_user(name, role="staff", **kw) -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, **kw).insert()


async def make_interview():
    owner = await make_user("Owen")
    acc = await Account(name="Alex", region="EU", country="PT", createdBy=owner.id).insert()
    rounds = [
        InterviewStageEvent(
            stage="intro", at=START, scheduledAt=START, endsAt=START.replace(hour=16), status="passed",
            note="TOPSECRET-NOTE", transcript="TOPSECRET-TRANSCRIPT", interviewerName="TOPSECRET-INTERVIEWER",
            caller=InterviewCaller(
                enabled=True, callerName="Cal", callerKind="external", startsAt=START, method="video",
                methodValue="TOPSECRET-LINK",
            ),
        ),
        InterviewStageEvent(
            stage="tech_round_1", at=START, scheduledAt=START.replace(day=9), status="scheduled",
            caller=InterviewCaller(enabled=False),
        ),
    ]
    iv = await Interview(
        accountId=acc.id, createdBy=owner.id, scheduledAt=START, companyName="Acme", appliedPosition="Dev",
        jobUrl="https://TOPSECRET-URL.example/job", note="TOPSECRET-NOTE",
        stageHistory=[r.model_dump() for r in rounds], stage="tech_round_1", status="scheduled",
    ).insert()
    return owner, acc, iv


def keys_anywhere(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            keys_anywhere(v, found)
    elif isinstance(value, list):
        for v in value:
            keys_anywhere(v, found)
    return found


async def test_staff_gets_summary_for_others_interview(client):
    owner, acc, iv = await make_interview()
    stranger = await make_user("Eve")
    async with client(stranger) as c:
        r = await c.get(f"/interviews/{iv.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["summaryOnly"] is True
    assert body["_id"] == str(iv.id)
    assert body["companyName"] == "Acme" and body["appliedPosition"] == "Dev"
    assert body["accountId"]["name"] == "Alex" and body["accountId"]["region"] == "EU"
    assert body["accountId"]["country"] == "PT"
    assert body["createdBy"]["name"] == "Owen"
    assert body["stage"] == "tech_round_1" and body["status"] == "scheduled"
    assert body["scheduledAt"]
    assert not {"note", "transcript", "resumeLink", "jobUrl", "interviewerName", "appliedBy", "method",
                "methodValue", "questions", "email"} & keys_anywhere(body)
    for word in SECRET_WORDS:
        assert word not in r.text
    first, second = body["stageHistory"]
    assert (first["stage"], first["status"]) == ("intro", "passed")
    assert first["scheduledAt"] and first["endsAt"] and first["id"]
    assert first["caller"] == {"enabled": True, "callerName": "Cal", "callerKind": "external"}
    assert second["stage"] == "tech_round_1" and second["caller"]["enabled"] is False


async def test_owner_still_gets_full_interview(client):
    owner, acc, iv = await make_interview()
    async with client(owner) as c:
        body = (await c.get(f"/interviews/{iv.id}")).json()
    assert "summaryOnly" not in body
    assert body["jobUrl"] and body["stageHistory"][0]["note"] == "TOPSECRET-NOTE"


async def test_admin_and_manager_still_get_full_interview(client):
    _, _, iv = await make_interview()
    for role in ("admin", "interview_manager"):
        user = await make_user(f"U{role}", role=role)
        async with client(user) as c:
            assert "summaryOnly" not in (await c.get(f"/interviews/{iv.id}")).json()


async def test_questions_still_forbidden_for_others(client):
    _, _, iv = await make_interview()
    stranger = await make_user("Eve")
    async with client(stranger) as c:
        assert (await c.get(f"/interviews/{iv.id}/questions")).status_code == 403
