from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.llm import rate_limiter
from app.llm.resolver import resolve_provider_preference, _is_allowlisted
from app.llm.types import PROVIDER_NVIDIA_FREE, PROVIDER_OPENAI
from app.resume_service import _resume_cache_key


def test_resolve_provider_preference_account_over_user():
    user = SimpleNamespace(llmProvider=PROVIDER_OPENAI)
    account = SimpleNamespace(llmProvider=PROVIDER_NVIDIA_FREE)
    assert resolve_provider_preference(user, account) == PROVIDER_NVIDIA_FREE


def test_resolve_provider_preference_inherits_user():
    user = SimpleNamespace(llmProvider=PROVIDER_NVIDIA_FREE)
    account = SimpleNamespace(llmProvider=None)
    assert resolve_provider_preference(user, account) == PROVIDER_NVIDIA_FREE


def test_is_allowlisted_user_or_account():
    uid = "u1"
    aid = "a1"
    assert _is_allowlisted(uid, aid, [uid], []) is True
    assert _is_allowlisted(uid, aid, [], [aid]) is True
    assert _is_allowlisted(uid, aid, [], []) is False


def test_resume_cache_key_includes_provider():
    a = _resume_cache_key("prompt", "openai", "gpt-5-mini")
    b = _resume_cache_key("prompt", "nvidia_free", "deepseek-ai/deepseek-v4-pro")
    assert a != b


def test_rate_limiter_acquire_respects_cap():
    rate_limiter.reset_for_tests()
    assert rate_limiter.acquire(2) is True
    assert rate_limiter.acquire(2) is True
    assert rate_limiter.acquire(2) is False
    assert rate_limiter.is_exhausted(2) is True


def test_complete_text_falls_back_on_429():
    import asyncio
    from httpx import HTTPStatusError, Request, Response

    from app.llm.client import complete_text
    from app.llm.types import EndpointConfig, ResolvedLlmConfig

    primary = EndpointConfig(
        provider=PROVIDER_NVIDIA_FREE,
        api_key="nv",
        model="deepseek-ai/deepseek-v4-pro",
        base_url="https://integrate.api.nvidia.com/v1",
    )
    backup = EndpointConfig(
        provider=PROVIDER_OPENAI,
        api_key="sk-test",
        model="gpt-5-mini",
    )
    config = ResolvedLlmConfig(
        primary=primary,
        backup=backup,
        requested_provider=PROVIDER_NVIDIA_FREE,
        effective_provider=PROVIDER_NVIDIA_FREE,
    )

    async def fake_invoke(endpoint, prompt):
        if endpoint.provider == PROVIDER_NVIDIA_FREE:
            req = Request("POST", "https://example.com")
            resp = Response(429)
            raise HTTPStatusError("rate limited", request=req, response=resp)
        return "backup-ok", {"input": 1, "output": 2, "reasoning": 0}

    async def run():
        with patch("app.llm.client._invoke", side_effect=fake_invoke):
            return await complete_text(config, "hello")

    result = asyncio.run(run())
    assert result.text == "backup-ok"
    assert result.fallback_used is True
