"""Anthropic Messages API adapter for the selectable-model router.

Sonnet 5.5 / Opus 5.5 reject `thinking` toggles and non-default sampling params, so the request
carries only model, max_tokens, the prompt and (optionally) `output_config.effort`.
"""

from __future__ import annotations

from typing import Optional

from anthropic import AsyncAnthropic

from app.config import get_settings

ANTHROPIC_TIMEOUT_SECONDS = 300


class ModelRefused(RuntimeError):
    """The model declined the request (stop_reason == "refusal")."""


class OutputTruncated(RuntimeError):
    """The output hit max_tokens, so it can't be trusted as a complete document."""


async def anthropic_complete(
    api_model: str,
    prompt: str,
    *,
    effort: Optional[str] = None,
    max_tokens: int = 16000,
    timeout: float = ANTHROPIC_TIMEOUT_SECONDS,
) -> tuple[str, dict]:
    settings = get_settings()
    api_key = (settings.ANTHROPIC_API_KEY or "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    client_kwargs: dict = {"api_key": api_key, "timeout": timeout}
    # User-level keys (sk-ant-usr-...) aren't tied to a workspace, so each request names one.
    workspace_id = (getattr(settings, "ANTHROPIC_WORKSPACE_ID", None) or "").strip()
    if workspace_id:
        client_kwargs["default_headers"] = {"anthropic-workspace-id": workspace_id}
    client = AsyncAnthropic(**client_kwargs)
    kwargs: dict = {
        "model": api_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if effort:
        kwargs["output_config"] = {"effort": effort}
    async with client.messages.stream(**kwargs) as stream:
        message = await stream.get_final_message()
    if message.stop_reason == "refusal":
        raise ModelRefused(f"{api_model} declined the request")
    if message.stop_reason == "max_tokens":
        raise OutputTruncated(f"{api_model} output was cut off at max_tokens")
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    usage = {
        "input": int(message.usage.input_tokens or 0),
        "output": int(message.usage.output_tokens or 0),
    }
    return text, usage
