"""GET /resume/models and require_model (no database: router on a bare app, auth overridden)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app import model_catalog as mc
from app.deps import get_current_user
from app.routers import resume as resume_router
from app.routers._util import require_model


def settings(**over):
    base = dict(
        ANTHROPIC_API_KEY="sk-ant-test",
        OPENAI_API_KEY=None,
        RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
        COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5",
        SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def use_settings(monkeypatch):
    def apply(**over):
        monkeypatch.setattr(mc, "get_settings", lambda: settings(**over))

    apply()
    return apply


@pytest.fixture
def make_client():
    clients = []

    def make(authed: bool = True, user=None):
        app = FastAPI()
        app.include_router(resume_router.router)
        if authed:
            fake = user or SimpleNamespace(freeLlmModelId=None, freeLlmApiKey=None, freeLlmMaxTokens=None)
            app.dependency_overrides[get_current_user] = lambda: fake
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


async def test_models_requires_auth(use_settings, make_client):
    async with make_client(authed=False) as client:
        r = await client.get("/resume/models", params={"task": "resume"})
    assert r.status_code == 401


async def test_models_lists_anthropic_when_key_configured(use_settings, make_client):
    async with make_client() as client:
        r = await client.get("/resume/models", params={"task": "resume"})
    assert r.status_code == 200
    body = r.json()
    assert body["task"] == "resume" and body["defaultId"] == "anthropic:claude-sonnet-5-5"
    ids = [m["id"] for m in body["models"]]
    assert "anthropic:claude-sonnet-5-5" in ids and "anthropic:claude-haiku-4-5" in ids
    assert [m["id"] for m in body["models"] if m["isDefault"]] == ["anthropic:claude-sonnet-5-5"]
    assert set(body["models"][0]) == {
        "id", "label", "provider", "tier", "bestFor", "estCostUsd", "isDefault", "recommended", "tag",
    }
    assert body["providers"] == [{"id": "anthropic", "label": "Anthropic"}]


async def test_models_list_both_providers_in_preset_order(use_settings, make_client):
    use_settings(OPENAI_API_KEY="sk-openai-test")
    async with make_client() as client:
        r = await client.get("/resume/models", params={"task": "screening"})
    body = r.json()
    assert body["providers"] == [
        {"id": "anthropic", "label": "Anthropic"}, {"id": "openai", "label": "OpenAI"},
    ]
    assert [m["id"] for m in body["models"] if m["provider"] == "openai"] == [
        "openai:gpt-5.4-mini", "openai:gpt-6-astra", "openai:gpt-5-mini", "openai:gpt-6-luna",
    ]
    assert [m["id"] for m in body["models"] if m["recommended"]] == [
        "anthropic:claude-sonnet-5-5", "openai:gpt-5.4-mini",
    ]


async def test_models_cover_letter_excludes_haiku_and_nvidia(use_settings, make_client):
    user = SimpleNamespace(freeLlmModelId="glm-5.3", freeLlmApiKey="nvapi-x", freeLlmMaxTokens=None)
    async with make_client(user=user) as client:
        r = await client.get("/resume/models", params={"task": "cover_letter"})
    ids = [m["id"] for m in r.json()["models"]]
    assert ids and not [i for i in ids if "haiku" in i or i.startswith("free:")]
    assert "anthropic:claude-fable-5-1" in ids
    assert r.json()["defaultId"] == "anthropic:claude-opus-5-5"


async def test_models_empty_when_nothing_configured(use_settings, make_client):
    use_settings(ANTHROPIC_API_KEY=None, OPENAI_API_KEY=None)
    async with make_client() as client:
        r = await client.get("/resume/models", params={"task": "resume"})
    assert r.status_code == 200
    assert r.json() == {"task": "resume", "defaultId": None, "providers": [], "models": []}


async def test_models_invalid_task_422(use_settings, make_client):
    async with make_client() as client:
        r = await client.get("/resume/models", params={"task": "poetry"})
    assert r.status_code == 422


def test_require_model_blank_is_none_and_valid_is_stripped(use_settings):
    assert require_model("resume", None, None) is None
    assert require_model("resume", "  ", None) is None
    assert require_model("resume", " anthropic:claude-sonnet-5-5 ", None) == "anthropic:claude-sonnet-5-5"


def test_require_model_maps_unavailable_to_400(use_settings):
    with pytest.raises(HTTPException) as exc:
        require_model("resume", "anthropic:nope", None)
    assert exc.value.status_code == 400 and "anthropic:nope" in exc.value.detail
