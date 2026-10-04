"""GET /accounts/lookup must not download whole profile documents (HTML templates are huge)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.deps import get_current_user
from app.routers import accounts as accounts_router

OWNER_ID = ObjectId()
ACCOUNT_ID = ObjectId()


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)


class FakeCollection:
    def __init__(self, rows):
        self.rows = rows
        self.pipelines = []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return FakeCursor(self.rows)


@pytest.fixture
def collection(monkeypatch):
    col = FakeCollection([])

    class FakeAccount:
        @staticmethod
        def get_motor_collection():
            return col

        @staticmethod
        def find(*a, **k):  # the old full-document path must not be used
            raise AssertionError("lookup must not load whole Account documents")

    monkeypatch.setattr(accounts_router, "Account", FakeAccount)
    return col


async def lookup():
    app = FastAPI()
    app.include_router(accounts_router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=OWNER_ID, role="staff")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/accounts/lookup")


async def test_lookup_keeps_the_same_response_shape(collection):
    collection.rows = [
        {
            "_id": ACCOUNT_ID, "name": "Backend", "country": "US", "region": "US", "createdBy": OWNER_ID,
            "hasTemplate": True, "hasPrompt": False, "showInGenerate": False, "archived": True,
        }
    ]
    r = await lookup()
    assert r.status_code == 200, r.text
    assert r.json() == {
        "accounts": [
            {
                "_id": str(ACCOUNT_ID), "name": "Backend", "country": "US", "region": "US",
                "hasTemplate": True, "hasPrompt": False, "createdBy": str(OWNER_ID),
                "showInGenerate": False, "archived": True,
            }
        ]
    }


async def test_lookup_defaults_for_profiles_saved_before_those_fields_existed(collection):
    collection.rows = [{"_id": ACCOUNT_ID, "name": "Old", "createdBy": OWNER_ID, "hasTemplate": False, "hasPrompt": False}]
    (row,) = (await lookup()).json()["accounts"]
    assert row["country"] is None and row["region"] is None
    assert row["showInGenerate"] is True and row["archived"] is False


async def test_lookup_projects_flags_in_the_database_and_never_returns_the_html(collection):
    await lookup()
    (pipeline,) = collection.pipelines
    (stage,) = pipeline
    projected = stage["$project"]
    assert set(projected) == {
        "name", "country", "region", "createdBy", "showInGenerate", "archived", "hasTemplate", "hasPrompt",
    }
    for heavy in ("styleTemplate", "previewHtml", "resumes", "resumePromptBody", "screeningPrompt", "coverLetterPrompt"):
        assert heavy not in projected  # only read inside the flag expressions, never returned
    assert "$styleTemplate" in str(projected["hasTemplate"]) and "$resumePromptBody" in str(projected["hasPrompt"])
