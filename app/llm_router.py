"""LLM routing: NVIDIA free tier primary, OpenAI fallback for resume + screening."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Literal, Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.config import get_settings
from app.free_llm import (
    FREE_LLM_RESUME_MAX_OUTPUT_TOKENS,
    FREE_LLM_RESUME_TIMEOUT_SECONDS,
    FREE_LLM_SCREENING_TIMEOUT_SECONDS,
    NVIDIA_BASE_URL,
    FreeLlmPreset,
    build_free_chat_kwargs,
    clamp_max_tokens,
    get_preset,
)
from app.llm_anthropic import anthropic_complete
from app.model_catalog import (
    TASK_EFFORT,
    ModelSpec,
    Task,
    fallback_chain,
    is_inactive,
    usage_cost,
    validate_requested,
)
from app.models import UserExtra

log = logging.getLogger(__name__)

LlmProvider = Literal["free", "openai", "anthropic"]


@dataclass
class LlmResult:
    text: str
    provider: LlmProvider
    model: str
    usage: dict
    fallback_used: bool
    fallback_reason: Optional[str] = None
    model_id: str = ""  # provider-prefixed catalog id, e.g. "anthropic:claude-sonnet-5-5"
    cost_usd: Optional[float] = None  # None when the model's price is unknown


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


def _free_timeout_for_purpose(purpose: str, preset: Optional[FreeLlmPreset] = None) -> float:
    if purpose == "resume":
        base = float(FREE_LLM_RESUME_TIMEOUT_SECONDS)
    elif purpose == "screening":
        base = float(FREE_LLM_SCREENING_TIMEOUT_SECONDS)
    else:
        base = float(FREE_LLM_RESUME_TIMEOUT_SECONDS)
    floor = float(preset.min_timeout_seconds) if preset and preset.min_timeout_seconds else 0.0
    return max(base, floor)


def _free_max_tokens_for_purpose(purpose: str, max_tokens: int) -> int:
    if purpose == "resume":
        return min(max_tokens, FREE_LLM_RESUME_MAX_OUTPUT_TOKENS)
    return max_tokens


def _message_text(message: object) -> str:
    """Final assistant text only — never use Kimi reasoning_content as the answer."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if isinstance(part, dict):
                if part.get("type") in (None, "text"):
                    parts.append(str(part.get("text") or ""))
                continue
            text = getattr(part, "text", None)
            if text:
                parts.append(str(text))
        return "".join(parts).strip()
    return (str(content).strip() if content else "")


async def _free_chat_completion(
    prompt: str,
    preset: FreeLlmPreset,
    api_key: str,
    max_tokens: int,
    *,
    timeout: float = FREE_LLM_RESUME_TIMEOUT_SECONDS,
    temperature: Optional[float] = None,
) -> tuple[str, dict]:
    client = AsyncOpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, timeout=timeout)
    kwargs = build_free_chat_kwargs(preset, prompt, max_tokens)
    if temperature is not None:
        kwargs["temperature"] = temperature
    response = await client.chat.completions.create(**kwargs)
    text = ""
    if response.choices:
        text = _message_text(response.choices[0].message)
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


# Purposes whose output must not vary between runs of the same input (structured extraction, not writing).
DETERMINISTIC_PURPOSES = frozenset({"job_extract"})


def _temperature_for(purpose: str) -> Optional[float]:
    return 0.0 if purpose in DETERMINISTIC_PURPOSES else None


def _openai_model_for(purpose: str, settings) -> str:
    if purpose == "job_extract" and (getattr(settings, "JOB_EXTRACT_MODEL", "") or "").strip():
        return settings.JOB_EXTRACT_MODEL.strip()
    return settings.RESUME_OPENAI_MODEL


_REASONING_FAMILIES = ("gpt-5", "gpt-6")


async def _openai_responses(
    prompt: str, model: Optional[str] = None, effort: Optional[str] = None, *, temperature: Optional[float] = None
) -> tuple[str, dict]:
    """One OpenAI Responses call. Reasoning effort goes only to the gpt-5/gpt-6 families; `effort`
    defaults to the RESUME_REASONING_EFFORT setting (legacy callers pass none). `temperature` goes to other
    models (o-series reasoning models reject it)."""
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    resolved = model or settings.RESUME_OPENAI_MODEL
    kwargs: dict = {"model": resolved, "input": prompt}
    if resolved.startswith(_REASONING_FAMILIES):
        resolved_effort = effort or (getattr(settings, "RESUME_REASONING_EFFORT", None) or "low").strip()
        kwargs["reasoning"] = {"effort": resolved_effort}
    elif temperature is not None and not resolved.startswith(("o1", "o3", "o4")):  # reasoning models reject it
        kwargs["temperature"] = temperature
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
    """Try free NVIDIA tier when configured; fall back to OpenAI on errors/bad output."""
    settings = get_settings()
    preset, max_tokens, api_key = resolve_free_preset(user) if user else (None, 8192, "")
    if purpose == "job_extract" and (getattr(settings, "JOB_EXTRACT_MODEL", "") or "").strip():
        preset = None  # the configured extraction model is the one whose accuracy was measured
    attempted_free = False
    fallback_reason: Optional[str] = None

    if preset and api_key:
        attempted_free = True
        try:
            free_timeout = _free_timeout_for_purpose(purpose, preset)
            free_max_tokens = _free_max_tokens_for_purpose(purpose, max_tokens)
            text, usage = await _free_chat_completion(
                prompt,
                preset,
                api_key,
                free_max_tokens,
                timeout=free_timeout,
                temperature=_temperature_for(purpose),
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

    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured and free LLM unavailable")

    openai_model = _openai_model_for(purpose, settings)
    text, usage = await _openai_responses(prompt, openai_model, temperature=_temperature_for(purpose))
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


async def _call_spec(
    spec: ModelSpec, prompt: str, task: Task, user: Optional[UserExtra]
) -> tuple[str, dict]:
    """One call to one catalog model. Raises on any provider failure."""
    if spec.provider == "anthropic":
        effort = TASK_EFFORT[task] if spec.supports_effort else None
        return await anthropic_complete(spec.api_model, prompt, effort=effort)
    return await _openai_responses(prompt, spec.api_model, effort=TASK_EFFORT[task])


def _failure_reason(spec: ModelSpec, exc: Exception) -> str:
    return f"{spec.label}: {str(exc)[:300]}"


async def complete(
    task: Task,
    prompt: str,
    user: Optional[UserExtra],
    *,
    model_id: Optional[str] = None,
    validate: Optional[Callable[[str], None]] = None,
) -> LlmResult:
    """Run `prompt` on the requested model (or the task default), falling back down the chain.

    Raises ModelNotAvailable for a bad requested id; otherwise re-raises the last model's error
    when every model in the chain fails. A model switched off after the request was saved (a queued
    job) gives way to the task default."""
    if is_inactive(model_id):
        log.info("%s: %s is turned off; using the default model", task, model_id)
        model_id = None
    validate_requested(task, model_id, user)
    chain = fallback_chain(task, model_id, user)
    if not chain:
        raise RuntimeError(f"No model available for {task}: configure an API key")
    first_reason: Optional[str] = None
    last_exc: Optional[Exception] = None
    for index, spec in enumerate(chain):
        try:
            text, usage = await _call_spec(spec, prompt, task, user)
            if not (text or "").strip():
                raise ValueError("empty model output")
            if validate:
                validate(text)
        except Exception as exc:  # noqa: BLE001 — any failure moves on to the next model
            last_exc = exc
            first_reason = first_reason or _failure_reason(spec, exc)
            log.warning("%s via %s failed: %s", task, spec.id, exc)
            continue
        log.info(
            "%s via %s in=%s out=%s fallback=%s",
            task, spec.id, usage.get("input", "?"), usage.get("output", "?"), index > 0,
        )
        return LlmResult(
            text=text,
            provider=spec.provider,
            model=spec.api_model,
            usage=usage,
            fallback_used=index > 0,
            fallback_reason=first_reason if index > 0 else None,
            model_id=spec.id,
            cost_usd=usage_cost(spec, usage),
        )
    assert last_exc is not None
    raise last_exc


async def complete_resume(
    prompt: str, user: Optional[UserExtra], model_id: Optional[str] = None
) -> LlmResult:
    return await complete("resume", prompt, user, model_id=model_id)


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


def _escape_raw_controls_in_json_strings(text: str) -> str:
    """Escape raw newlines/tabs inside JSON strings (common free-LLM bug).

    Valid JSON forbids unescaped control characters in strings; models often
    emit real newlines inside quoted answer text. Walk the text and escape
    those controls only while inside a double-quoted string.
    """
    out: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def _loads_json_lenient(text: str) -> object | None:
    """json.loads, then retry after escaping raw controls inside strings."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    repaired = _escape_raw_controls_in_json_strings(text)
    if repaired == text:
        return None
    try:
        return json.loads(repaired)
    except (json.JSONDecodeError, ValueError):
        return None


def _try_parse_jsonish(raw: str) -> object | None:
    """Parse JSON, or common near-JSON / Python-literal payloads from LLMs."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
    if not cleaned:
        return None
    parsed = _loads_json_lenient(cleaned)
    if parsed is not None:
        return parsed
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
            parsed = _loads_json_lenient(snippet)
            if parsed is not None:
                return parsed
            try:
                import ast

                value = ast.literal_eval(snippet)
                if isinstance(value, (dict, list)):
                    return value
            except (SyntaxError, ValueError):
                pass
    return None


def _looks_like_answers_wrapper_blob(text: str) -> bool:
    """True when an answer string is still a full {"answers":[...]} JSON dump."""
    t = (text or "").strip()
    if not t.startswith("{"):
        return False
    # Peek near the start so long answer prose with the word "answers" is fine.
    head = t[:120].replace(" ", "")
    return '"answers"' in head or "'answers'" in head


def _answers_from_parsed(parsed: object) -> list[str]:
    """Extract a list of plain answer strings from a parsed LLM payload.

    Handles double-encoded JSON strings and full `{"answers":[...]}` blobs
    stuffed into a single string element (common free-LLM wrapping).
    """
    if isinstance(parsed, dict):
        ans = parsed.get("answers")
        if isinstance(ans, list):
            # Full payload mistakenly nested as the sole answers[0] element.
            if len(ans) == 1:
                only = ans[0]
                if isinstance(only, str):
                    nested = _try_parse_jsonish(only.strip())
                    if nested is not None:
                        expanded = _answers_from_parsed(nested)
                        if len(expanded) > 1 or (expanded and expanded[0] and expanded[0] != only.strip()):
                            return expanded
                elif isinstance(only, (dict, list)):
                    expanded = _answers_from_parsed(only)
                    if len(expanded) > 1:
                        return expanded
            return [_coerce_screening_answer(a) for a in ans]
        if isinstance(ans, str):
            nested = _try_parse_jsonish(ans.strip())
            if nested is not None:
                return _answers_from_parsed(nested)
        # Single Q&A object mistakenly returned as the whole payload.
        if "answer" in parsed:
            return [_coerce_screening_answer(parsed)]
        return []
    if isinstance(parsed, list):
        if len(parsed) == 1:
            only = parsed[0]
            if isinstance(only, str):
                nested = _try_parse_jsonish(only.strip())
                if nested is not None:
                    expanded = _answers_from_parsed(nested)
                    if len(expanded) > 1:
                        return expanded
            elif isinstance(only, (dict, list)):
                expanded = _answers_from_parsed(only)
                if len(expanded) > 1:
                    return expanded
        return [_coerce_screening_answer(a) for a in parsed]
    if isinstance(parsed, str):
        text = parsed.strip()
        if not text:
            return []
        nested = _try_parse_jsonish(text)
        # Recurse when the string unwraps to a structured payload (double-encoded
        # JSON). Avoid infinite loops on plain strings that parse to themselves.
        if nested is not None and not (isinstance(nested, str) and nested == text):
            return _answers_from_parsed(nested)
        coerced = _coerce_screening_answer(parsed)
        return [coerced] if coerced else []
    return []


def _expand_if_single_payload(answers: list[str]) -> list[str]:
    """If fallback left one blob that is a full answers JSON, expand it."""
    non_empty = [a for a in answers if (a or "").strip()]
    if len(non_empty) != 1:
        return answers
    nested = _try_parse_jsonish(non_empty[0].strip())
    if nested is None:
        return answers
    expanded = _answers_from_parsed(nested)
    if len(expanded) > 1 and any(a.strip() for a in expanded):
        return expanded
    return answers


def parse_screening_answers(raw: str, questions: list[str]) -> list[str]:
    # Lazy import: resume_service imports this module at top level.
    from app.resume_service import _normalize_dashes

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
        answers = _expand_if_single_payload(answers)
    else:
        answers = _expand_if_single_payload(answers)
    while len(answers) < len(questions):
        answers.append("")
    return [_normalize_dashes(a) for a in answers[: len(questions)]]


def _validate_screening_output(raw: str, questions: list[str]) -> None:
    answers = parse_screening_answers(raw, questions)
    if not any(a.strip() for a in answers):
        raise ValueError("screening LLM returned no parseable answers")
    # Reject leftover wrapper dumps so free→OpenAI fallback retries instead of
    # storing raw JSON (with visible \n escapes) as answer text.
    if any(_looks_like_answers_wrapper_blob(a) for a in answers):
        raise ValueError("screening LLM returned unparsed answers JSON wrapper")


async def complete_screening(
    prompt: str,
    user: Optional[UserExtra],
    questions: list[str],
    model_id: Optional[str] = None,
) -> LlmResult:
    def _validate(raw: str) -> None:
        _validate_screening_output(raw, questions)

    return await complete("screening", prompt, user, model_id=model_id, validate=_validate)


async def test_free_connection(user: UserExtra) -> dict:
    """Lightweight ping against the user's configured free model."""
    preset, max_tokens, api_key = resolve_free_preset(user)
    if preset is None or not api_key:
        raise ValueError("Free LLM model and API key must be configured")
    test_timeout = float(preset.test_timeout_seconds)
    try:
        text, _usage = await _free_chat_completion(
            "Reply with exactly: ok",
            preset,
            api_key,
            min(max_tokens, preset.test_max_tokens),
            timeout=test_timeout,
        )
    except Exception as exc:
        raise ValueError(_free_llm_user_message(exc, timeout_seconds=test_timeout)) from exc
    if not text.strip():
        raise ValueError("Model returned empty response")
    return {"ok": True, "model": preset.model, "sample": text.strip()[:80]}
