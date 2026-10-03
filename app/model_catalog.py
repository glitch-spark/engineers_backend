"""Selectable LLM catalog: which models exist, which are usable right now, per-task defaults,
fallback order and cost estimates. Pure data + lookups; no network calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from app.config import get_settings
from app.free_llm import FREE_LLM_PRESETS, clamp_max_tokens, get_preset
from app.models import UserExtra

Task = Literal["resume", "cover_letter", "screening"]
Tier = Literal["free", "budget", "balanced", "premium"]
Provider = Literal["anthropic", "openai", "free"]

ALL_TASKS: frozenset[Task] = frozenset({"resume", "cover_letter", "screening"})
_RESUME_AND_SCREENING: frozenset[Task] = frozenset({"resume", "screening"})

# Anthropic effort per task (output_config.effort); only sent to models with supports_effort.
TASK_EFFORT: dict[Task, str] = {"resume": "low", "cover_letter": "high", "screening": "low"}

# Typical (input, output) tokens for one task run, for the UI cost estimate. Cover letter covers
# the whole draft + critique + revise pipeline.
TASK_TOKEN_PROFILE: dict[Task, tuple[int, int]] = {
    "resume": (6000, 5000),
    "cover_letter": (21000, 1700),
    "screening": (4000, 800),
}


class ModelNotAvailable(ValueError):
    """The requested model id is unknown, not allowed for the task, or its provider isn't configured."""


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: Provider
    api_model: str
    label: str
    tier: Tier
    tasks: frozenset[Task]
    best_for: str
    price_in: Optional[float]  # USD per 1M input tokens; None = unknown
    price_out: Optional[float]  # USD per 1M output tokens; None = unknown
    supports_effort: bool = False


def _anthropic_specs() -> list[ModelSpec]:
    return [
        ModelSpec(
            "anthropic:claude-haiku-4-5", "anthropic", "claude-haiku-4-5", "Haiku 4.5", "budget",
            _RESUME_AND_SCREENING, "Cheapest Claude; fine for simple screening answers", 1.0, 5.0,
        ),
        ModelSpec(
            "anthropic:claude-sonnet-5-5", "anthropic", "claude-sonnet-5-5", "Sonnet 5.5", "balanced",
            ALL_TASKS, "Best value: precise template edits and solid prose", 2.0, 10.0, True,
        ),
        ModelSpec(
            "anthropic:claude-opus-5-5", "anthropic", "claude-opus-5-5", "Opus 5.5", "premium",
            ALL_TASKS, "Top quality writing and judgment", 4.0, 20.0, True,
        ),
        ModelSpec(
            "anthropic:claude-fable-5-1", "anthropic", "claude-fable-5-1", "Fable 5.1", "premium",
            frozenset({"cover_letter"}), "Maximum quality, most expensive", 10.0, 50.0, True,
        ),
    ]


def _openai_spec() -> ModelSpec:
    model = get_settings().RESUME_OPENAI_MODEL
    return ModelSpec(
        f"openai:{model}", "openai", model, model, "budget", ALL_TASKS,
        "Server-configured OpenAI model", None, None,
    )


def _free_specs(user: Optional[UserExtra]) -> list[ModelSpec]:
    if user is None or not (getattr(user, "freeLlmApiKey", None) or "").strip():
        return []
    return [
        ModelSpec(
            f"free:{p.id}", "free", p.model, p.label, "free", _RESUME_AND_SCREENING,
            "Free NVIDIA tier; slower and less consistent", 0.0, 0.0,
        )
        for p in FREE_LLM_PRESETS
    ]


def _provider_configured(provider: Provider, user: Optional[UserExtra]) -> bool:
    settings = get_settings()
    if provider == "anthropic":
        return bool((settings.ANTHROPIC_API_KEY or "").strip())
    if provider == "openai":
        return bool((settings.OPENAI_API_KEY or "").strip())
    return bool(_free_specs(user))


def _all_specs(user: Optional[UserExtra]) -> list[ModelSpec]:
    return [*_anthropic_specs(), _openai_spec(), *_free_specs(user)]


def available_models(task: Task, user: Optional[UserExtra]) -> list[ModelSpec]:
    return [
        s for s in _all_specs(user)
        if task in s.tasks and _provider_configured(s.provider, user)
    ]


def get_model(model_id: str, user: Optional[UserExtra]) -> Optional[ModelSpec]:
    """Any task, only if its provider is usable right now."""
    for spec in _all_specs(user):
        if spec.id == model_id and _provider_configured(spec.provider, user):
            return spec
    return None


def _configured_default(task: Task) -> str:
    settings = get_settings()
    return {
        "resume": settings.RESUME_DEFAULT_MODEL,
        "cover_letter": settings.COVER_LETTER_DEFAULT_MODEL,
        "screening": settings.SCREENING_DEFAULT_MODEL,
    }[task]


def _saved_free_id(task: Task, user: Optional[UserExtra]) -> Optional[str]:
    """The user's saved NVIDIA preset, preferred for resume/screening (today's behavior)."""
    if task not in _RESUME_AND_SCREENING or user is None:
        return None
    preset = get_preset(getattr(user, "freeLlmModelId", None))
    if preset is None or not _free_specs(user):
        return None
    return f"free:{preset.id}"


def default_model_id(task: Task, user: Optional[UserExtra]) -> Optional[str]:
    free_id = _saved_free_id(task, user)
    if free_id:
        return free_id
    configured = _configured_default(task)
    usable = {s.id for s in available_models(task, user)}
    if configured in usable:
        return configured
    openai_id = _openai_spec().id
    return openai_id if openai_id in usable else None


def validate_requested(task: Task, model_id: Optional[str], user: Optional[UserExtra]) -> Optional[ModelSpec]:
    wanted = (model_id or "").strip()
    if not wanted:
        return None
    spec = next((s for s in _all_specs(user) if s.id == wanted), None)
    if spec is None:
        raise ModelNotAvailable(f"Unknown model '{wanted}'.")
    if task not in spec.tasks:
        raise ModelNotAvailable(f"Model '{wanted}' can't be used for {task.replace('_', ' ')}.")
    if not _provider_configured(spec.provider, user):
        raise ModelNotAvailable(f"Model '{wanted}' is not configured on this server.")
    return spec


def fallback_chain(task: Task, requested_id: Optional[str], user: Optional[UserExtra]) -> list[ModelSpec]:
    """Preferred model, then the task default, then the configured OpenAI model; usable ones only."""
    usable = {s.id: s for s in available_models(task, user)}
    wanted = (requested_id or "").strip() or default_model_id(task, user)
    order = [wanted, _configured_default(task), _openai_spec().id]
    chain: list[ModelSpec] = []
    for model_id in order:
        spec = usable.get(model_id or "")
        if spec is not None and spec not in chain:
            chain.append(spec)
    return chain


def cache_lane(spec: ModelSpec, task: Task, user: Optional[UserExtra]) -> str:
    """Key part for the resume LLM output cache; kept identical to the legacy openai/free lanes."""
    if spec.provider == "openai":
        effort = (getattr(get_settings(), "RESUME_REASONING_EFFORT", None) or "low").strip()
        return f"openai:{spec.api_model}:{effort}"
    if spec.provider == "free":
        preset = get_preset(spec.id.split(":", 1)[1])
        max_tokens = clamp_max_tokens(getattr(user, "freeLlmMaxTokens", None), preset)
        return f"free:{spec.api_model}:{max_tokens}"
    effort = TASK_EFFORT[task] if spec.supports_effort else "none"
    return f"anthropic:{spec.api_model}:{effort}"


def usage_cost(spec: ModelSpec, usage: dict) -> Optional[float]:
    if spec.price_in is None or spec.price_out is None:
        return None
    return (
        (usage.get("input") or 0) * spec.price_in + (usage.get("output") or 0) * spec.price_out
    ) / 1_000_000


def estimate_call_cost(spec: ModelSpec, task: Task) -> Optional[float]:
    tokens_in, tokens_out = TASK_TOKEN_PROFILE[task]
    return usage_cost(spec, {"input": tokens_in, "output": tokens_out})


def public_view(spec: ModelSpec, task: Task, default_id: Optional[str]) -> dict:
    return {
        "id": spec.id,
        "label": spec.label,
        "provider": spec.provider,
        "tier": spec.tier,
        "bestFor": spec.best_for,
        "estCostUsd": estimate_call_cost(spec, task),
        "isDefault": spec.id == default_id,
    }
