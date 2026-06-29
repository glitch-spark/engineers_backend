from __future__ import annotations

import logging
from typing import Optional

import httpx
from fastapi import HTTPException, status
from openai import AsyncOpenAI

from app.llm import rate_limiter
from app.llm.settings import get_or_create_llm_settings
from app.llm.types import (
    PROVIDER_NVIDIA_FREE,
    PROVIDER_OPENAI,
    EndpointConfig,
    LlmCompletionResult,
    ResolvedLlmConfig,
)

log = logging.getLogger(__name__)

_FALLBACK_STATUS = {429, 502, 503, 504}


async def _call_nvidia(endpoint: EndpointConfig, prompt: str) -> tuple[str, dict]:
    doc = await get_or_create_llm_settings()
    if not rate_limiter.acquire(doc.nvidiaMaxRpm):
        raise httpx.HTTPStatusError(
            "rate limit pre-check",
            request=httpx.Request("POST", endpoint.base_url or ""),
            response=httpx.Response(429),
        )

    base = (endpoint.base_url or "https://integrate.api.nvidia.com/v1").rstrip("/")
    url = f"{base}/chat/completions"
    payload = {
        "model": endpoint.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": endpoint.max_tokens,
        "temperature": endpoint.temperature,
        "top_p": endpoint.top_p,
        "stream": False,
    }
    if endpoint.extra_body:
        payload.update(endpoint.extra_body)

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {endpoint.api_key}",
                "Accept": "application/json",
            },
            json=payload,
        )
    if resp.status_code >= 400:
        resp.raise_for_status()

    data = resp.json()
    content = ""
    choices = data.get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content") or ""
    usage_raw = data.get("usage") or {}
    usage = {
        "input": int(usage_raw.get("prompt_tokens") or 0),
        "output": int(usage_raw.get("completion_tokens") or 0),
        "reasoning": 0,
    }
    return content, usage


async def _call_openai(endpoint: EndpointConfig, prompt: str) -> tuple[str, dict]:
    client = AsyncOpenAI(api_key=endpoint.api_key, base_url=endpoint.base_url or None)
    kwargs: dict = {"model": endpoint.model, "input": prompt}
    if endpoint.model.startswith("gpt-5"):
        kwargs["reasoning"] = {"effort": endpoint.reasoning_effort}
    response = await client.responses.create(**kwargs)

    usage_out: dict = {"input": 0, "output": 0, "reasoning": 0}
    usage = getattr(response, "usage", None)
    if usage is not None:
        out_details = getattr(usage, "output_tokens_details", None)
        reasoning_toks = getattr(out_details, "reasoning_tokens", None) if out_details else None
        input_toks = getattr(usage, "input_tokens", None)
        output_toks = getattr(usage, "output_tokens", None)
        if input_toks is not None:
            usage_out["input"] = int(input_toks)
        if output_toks is not None:
            usage_out["output"] = int(output_toks)
        if reasoning_toks is not None:
            usage_out["reasoning"] = int(reasoning_toks)
    return (response.output_text or ""), usage_out


async def _invoke(endpoint: EndpointConfig, prompt: str) -> tuple[str, dict]:
    if endpoint.provider == PROVIDER_NVIDIA_FREE:
        return await _call_nvidia(endpoint, prompt)
    return await _call_openai(endpoint, prompt)


def _should_fallback(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code if exc.response is not None else 0
        return code in _FALLBACK_STATUS
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    return False


async def complete_text(
    config: ResolvedLlmConfig,
    prompt: str,
    *,
    user_id: Optional[str] = None,
    account_id: Optional[str] = None,
) -> LlmCompletionResult:
    """Run prompt against resolved primary endpoint; fall back to OpenAI once."""
    primary = config.primary
    backup = config.backup

    try:
        text, usage = await _invoke(primary, prompt)
        if not (text or "").strip():
            raise ValueError("empty LLM output")
        return LlmCompletionResult(
            text=text,
            usage=usage,
            provider_used=primary.provider,
            model_used=primary.model,
        )
    except Exception as exc:
        if primary.provider == backup.provider and primary.model == backup.model:
            if isinstance(exc, httpx.HTTPStatusError):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"LLM request failed ({exc.response.status_code})",
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="LLM request failed",
            ) from exc

        if not _should_fallback(exc):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc

        reason = str(exc)
        log.warning(
            "LLM fallback user=%s account=%s primary=%s reason=%s",
            user_id,
            account_id,
            primary.provider,
            reason,
        )
        text, usage = await _invoke(backup, prompt)
        if not (text or "").strip():
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="LLM backup returned empty output",
            )
        return LlmCompletionResult(
            text=text,
            usage=usage,
            provider_used=backup.provider,
            model_used=backup.model,
            fallback_used=True,
            fallback_reason=reason,
        )
