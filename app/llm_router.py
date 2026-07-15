"""LLM routing: NVIDIA free tier primary, OpenAI fallback for resume + screening."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Literal, Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.config import get_settings
from app.free_llm import (
    DEFAULT_RPM_LIMIT,
    FREE_LLM_RESUME_MAX_OUTPUT_TOKENS,
    FREE_LLM_RESUME_TIMEOUT_SECONDS,
    FREE_LLM_SCREENING_TIMEOUT_SECONDS,
    FREE_LLM_TEST_TIMEOUT_SECONDS,
    NVIDIA_BASE_URL,
    RPM_WINDOW_SECONDS,
    FreeLlmPreset,
    clamp_max_tokens,
    get_preset,
)
from app.models import UserExtra

log = logging.getLogger(__name__)

LlmProvider = Literal["free", "openai"]

# In-process sliding window: api_key_hash -> deque[timestamp]
_rate_windows: dict[str, deque[float]] = defaultdict(deque)


@dataclass
class LlmResult:
    text: str
    provider: LlmProvider
    model: str
    usage: dict
    fallback_used: bool
    fallback_reason: Optional[str] = None


def _key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def rate_limit_allows(api_key: str, *, limit: int = DEFAULT_RPM_LIMIT) -> bool:
    """Return True if a free-tier call is allowed under the RPM window."""
    now = time.monotonic()
    window = _rate_windows[_key_hash(api_key)]
    cutoff = now - RPM_WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()
    return len(window) < limit


def rate_limit_record(api_key: str) -> None:
    _rate_windows[_key_hash(api_key)].append(time.monotonic())


def clear_rate_limits() -> None:
    """Test helper — reset all windows."""
    _rate_windows.clear()


def free_llm_configured(user: Optional[UserExtra]) -> bool:
    if user is None:
        return False
    preset = get_preset(user.freeLlmModelId)
    key = (user.freeLlmApiKey or "").strip()
    return preset is not None and bool(key)


def resolve_free_preset(user: UserExtra) -> tuple[Optional[FreeLlmPreset], int, str]:
    preset = get_preset(user.freeLlmModelId)
    if preset is None:
        return None, 8192, ""
    max_tokens = clamp_max_tokens(user.freeLlmMaxTokens, preset)
    api_key = (user.freeLlmApiKey or "").strip()
    return preset, max_tokens, api_key


def _free_llm_user_message(exc: Exception, *, timeout_seconds: float) -> str:
    """Map NVIDIA/OpenAI SDK errors to short user-facing messages."""
    if isinstance(exc, APITimeoutError):
        return (
            f"NVIDIA request timed out after {int(timeout_seconds)}s. "
            "The model may be slow — try again in a moment."
        )
    if isinstance(exc, APIConnectionError):
        return "Could not reach NVIDIA Integrate. Check your network connection."
    if isinstance(exc, RateLimitError):
        return "NVIDIA rate limit exceeded on your API key. Wait a minute and try again."
    if isinstance(exc, APIStatusError):
        if exc.status_code == 401:
            return "Invalid NVIDIA API key. Check that your nvapi-… key is correct and active."
        if exc.status_code == 403:
            return "NVIDIA API key rejected. Ensure the key has access to the selected model."
        detail = getattr(exc, "message", None) or str(exc)
        return f"NVIDIA API error ({exc.status_code}): {detail}"
    return f"Free LLM request failed: {exc}"


def _free_timeout_for_purpose(purpose: str) -> float:
    if purpose == "resume":
        return float(FREE_LLM_RESUME_TIMEOUT_SECONDS)
    if purpose == "screening":
        return float(FREE_LLM_SCREENING_TIMEOUT_SECONDS)
    return float(FREE_LLM_RESUME_TIMEOUT_SECONDS)


def _free_max_tokens_for_purpose(purpose: str, max_tokens: int) -> int:
    if purpose == "resume":
        return min(max_tokens, FREE_LLM_RESUME_MAX_OUTPUT_TOKENS)
    return max_tokens


async def _free_chat_completion(
    prompt: str,
    preset: FreeLlmPreset,
    api_key: str,
    max_tokens: int,
    *,
    timeout: float = FREE_LLM_RESUME_TIMEOUT_SECONDS,
) -> tuple[str, dict]:
    client = AsyncOpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, timeout=timeout)
    kwargs: dict = {
        "model": preset.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": preset.temperature,
        "top_p": preset.top_p,
    }
    if preset.extra_body:
        kwargs["extra_body"] = preset.extra_body
    response = await client.chat.completions.create(**kwargs)
    text = ""
    if response.choices:
        text = (response.choices[0].message.content or "").strip()
    usage_out: dict = {}
    if response.usage:
        if response.usage.prompt_tokens is not None:
            usage_out["input"] = int(response.usage.prompt_tokens)
        if response.usage.completion_tokens is not None:
            usage_out["output"] = int(response.usage.completion_tokens)
    return text, usage_out


def _free_error_retryable(exc: Exception) -> bool:
    if isinstance(exc, APIStatusError):
        code = exc.status_code
        return code == 429 or code >= 500
    # Timeouts, connection errors, etc.
    return True


async def _openai_responses(prompt: str, model: Optional[str] = None) -> tuple[str, dict]:
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    resolved = model or settings.RESUME_OPENAI_MODEL
    kwargs: dict = {"model": resolved, "input": prompt}
    if resolved.startswith("gpt-5"):
        effort = (getattr(settings, "RESUME_REASONING_EFFORT", None) or "low").strip()
        kwargs["reasoning"] = {"effort": effort}
    response = await client.responses.create(**kwargs)
    usage_out: dict = {}
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
    return (response.output_text or "").strip(), usage_out


async def complete_openai_only(prompt: str, *, purpose: str = "llm") -> LlmResult:
    """Force OpenAI (no free attempt). Used when free output fails validation."""
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")
    openai_model = settings.RESUME_OPENAI_MODEL
    text, usage = await _openai_responses(prompt, openai_model)
    log.info("%s via OpenAI-only model=%s", purpose, openai_model)
    return LlmResult(
        text=text,
        provider="openai",
        model=openai_model,
        usage=usage,
        fallback_used=True,
    )


async def complete_with_fallback(
    prompt: str,
    user: Optional[UserExtra],
    *,
    validate: Optional[Callable[[str], None]] = None,
    purpose: str = "llm",
) -> LlmResult:
    """Try free NVIDIA tier when configured; fall back to OpenAI on limit/errors/bad output."""
    settings = get_settings()
    preset, max_tokens, api_key = resolve_free_preset(user) if user else (None, 8192, "")
    attempted_free = False
    fallback_reason: Optional[str] = None

    if preset and api_key:
        if rate_limit_allows(api_key):
            attempted_free = True
            try:
                rate_limit_record(api_key)
                free_timeout = _free_timeout_for_purpose(purpose)
                free_max_tokens = _free_max_tokens_for_purpose(purpose, max_tokens)
                text, usage = await _free_chat_completion(
                    prompt,
                    preset,
                    api_key,
                    free_max_tokens,
                    timeout=free_timeout,
                )
                if not text.strip():
                    raise ValueError("empty free LLM output")
                if validate:
                    validate(text)
                log.info(
                    "%s via free model=%s in=%s out=%s",
                    purpose,
                    preset.model,
                    usage.get("input", "?"),
                    usage.get("output", "?"),
                )
                return LlmResult(
                    text=text,
                    provider="free",
                    model=preset.model,
                    usage=usage,
                    fallback_used=False,
                )
            except Exception as exc:
                fallback_reason = _free_llm_user_message(exc, timeout_seconds=free_timeout)
                log.warning(
                    "%s free LLM failed (model=%s): %s — falling back to OpenAI",
                    purpose,
                    preset.model,
                    exc,
                )
                if not _free_error_retryable(exc) and not validate:
                    pass
        else:
            log.info("%s free LLM rate limit reached — falling back to OpenAI", purpose)
            attempted_free = True
            fallback_reason = (
                "Free-tier rate limit reached (40 requests/min). "
                "Fell back to OpenAI for this request."
            )

    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured and free LLM unavailable")

    openai_model = settings.RESUME_OPENAI_MODEL
    text, usage = await _openai_responses(prompt, openai_model)
    if validate:
        validate(text)
    log.info(
        "%s via OpenAI model=%s fallback=%s in=%s out=%s",
        purpose,
        openai_model,
        attempted_free,
        usage.get("input", "?"),
        usage.get("output", "?"),
    )
    return LlmResult(
        text=text,
        provider="openai",
        model=openai_model,
        usage=usage,
        fallback_used=attempted_free,
        fallback_reason=fallback_reason,
    )


async def complete_resume(prompt: str, user: Optional[UserExtra]) -> LlmResult:
    return await complete_with_fallback(prompt, user, purpose="resume")


def _coerce_screening_answer(item: object) -> str:
    """Normalize one answer entry to plain text.

    Free / OpenAI models sometimes return objects instead of strings, e.g.
      {"question": "...", "answer": "Real text", "type_used": "long_text"}
    or Python-style dicts as strings. Always extract the answer text.
    """
    if item is None:
        return ""
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return ""
        # Already a stringified object/dict from a previous bad parse or model.
        if (text.startswith("{") and text.endswith("}")) or (
            text.startswith("[") and text.endswith("]")
        ):
            nested = _try_parse_jsonish(text)
            if nested is not None:
                return _coerce_screening_answer(nested)
        return text
    if isinstance(item, dict):
        for key in ("answer", "text", "response", "content", "value"):
            if key in item and item[key] is not None:
                return _coerce_screening_answer(item[key])
        # Last resort: avoid dumping the whole object into the UI.
        return ""
    if isinstance(item, (list, tuple)) and item:
        return _coerce_screening_answer(item[0])
    return str(item).strip()


def _try_parse_jsonish(raw: str) -> object | None:
    """Parse JSON, or common near-JSON / Python-literal payloads from LLMs."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass
    # Models sometimes emit Python dict/list literals (single quotes).
    try:
        import ast

        value = ast.literal_eval(cleaned)
        if isinstance(value, (dict, list, str)):
            return value
    except (SyntaxError, ValueError):
        pass
    # Salvage a JSON object/array embedded in prose.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start >= 0 and end > start:
            snippet = cleaned[start : end + 1]
            try:
                return json.loads(snippet)
            except (json.JSONDecodeError, ValueError):
                try:
                    import ast

                    value = ast.literal_eval(snippet)
                    if isinstance(value, (dict, list)):
                        return value
                except (SyntaxError, ValueError):
                    pass
    return None


def _answers_from_parsed(parsed: object) -> list[str]:
    if isinstance(parsed, dict):
        if isinstance(parsed.get("answers"), list):
            return [_coerce_screening_answer(a) for a in parsed["answers"]]
        # Single Q&A object mistakenly returned as the whole payload.
        if "answer" in parsed:
            return [_coerce_screening_answer(parsed)]
        return []
    if isinstance(parsed, list):
        return [_coerce_screening_answer(a) for a in parsed]
    if isinstance(parsed, str):
        coerced = _coerce_screening_answer(parsed)
        return [coerced] if coerced else []
    return []


def parse_screening_answers(raw: str, questions: list[str]) -> list[str]:
    answers: list[str] = []
    parsed = _try_parse_jsonish(raw)
    if parsed is not None:
        answers = _answers_from_parsed(parsed)
    # Drop empties only when we got a partial object dump; keep positional empties later.
    if answers and not any(a.strip() for a in answers):
        answers = []
    if not answers:
        chunks = re.split(r"^\s*\d+\.\s*", raw, flags=re.MULTILINE)
        answers = [_coerce_screening_answer(c) for c in chunks if c.strip()]
    while len(answers) < len(questions):
        answers.append("")
    return answers[: len(questions)]


def _validate_screening_output(raw: str, questions: list[str]) -> None:
    answers = parse_screening_answers(raw, questions)
    if not any(a.strip() for a in answers):
        raise ValueError("screening LLM returned no parseable answers")


async def complete_screening(prompt: str, user: Optional[UserExtra], questions: list[str]) -> LlmResult:
    def _validate(raw: str) -> None:
        _validate_screening_output(raw, questions)

    return await complete_with_fallback(
        prompt,
        user,
        validate=_validate,
        purpose="screening",
    )


async def test_free_connection(user: UserExtra) -> dict:
    """Lightweight ping against the user's configured free model."""
    preset, max_tokens, api_key = resolve_free_preset(user)
    if preset is None or not api_key:
        raise ValueError("Free LLM model and API key must be configured")
    if not rate_limit_allows(api_key):
        raise ValueError("Rate limit reached (~40 requests/minute). Try again shortly.")
    rate_limit_record(api_key)
    test_timeout = float(FREE_LLM_TEST_TIMEOUT_SECONDS)
    try:
        text, _usage = await _free_chat_completion(
            "Reply with exactly: ok",
            preset,
            api_key,
            min(max_tokens, 16),
            timeout=test_timeout,
        )
    except Exception as exc:
        raise ValueError(_free_llm_user_message(exc, timeout_seconds=test_timeout)) from exc
    if not text.strip():
        raise ValueError("Model returned empty response")
    return {"ok": True, "model": preset.model, "sample": text.strip()[:80]}
