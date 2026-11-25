"""Selectable LLM catalog: providers, the suggested models per task, which are usable right now,
defaults, fallback order and cost estimates. Pure data + lookups; no network calls.

Two providers (Anthropic, OpenAI). Each task offers an ordered preset list per provider; the first
active entry is that provider's recommendation for the task."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal, Optional

from app.config import get_settings
from app.models import UserExtra

Task = Literal["resume", "cover_letter", "screening", "judge"]
Tier = Literal["budget", "balanced", "premium"]
Provider = Literal["anthropic", "openai"]

PROVIDER_ORDER: tuple[Provider, ...] = ("anthropic", "openai")
PROVIDER_LABEL: dict[Provider, str] = {"anthropic": "Anthropic", "openai": "OpenAI"}

# Reasoning/thinking depth per task (Anthropic output_config.effort, OpenAI reasoning.effort).
TASK_EFFORT: dict[Task, str] = {"resume": "low", "cover_letter": "high", "screening": "low", "judge": "high"}

# Typical (input, output) tokens for one task run, for the UI cost estimate. Cover letter covers
# the whole draft + critique + revise pipeline. Judge = one resume (plain text) + one JD + rubric → JSON verdict.
TASK_TOKEN_PROFILE: dict[Task, tuple[int, int]] = {
    "resume": (6000, 5000),
    "cover_letter": (21000, 1700),
    "screening": (4000, 800),
    "judge": (7000, 1200),
}


class ModelNotAvailable(ValueError):
    """The requested model id is unknown, not offered for the task, or its provider isn't configured."""


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: Provider
    api_model: str
    label: str
    tier: Tier
    best_for: str
    price_in: float  # USD per 1M input tokens
    price_out: float  # USD per 1M output tokens
    supports_effort: bool = True
    # Off: never offered, defaulted to or fallen back to; requests saved with it use the task default instead.
    active: bool = True
    # The provider's retirement date (UTC): from that day on the model counts as off. Its price is kept for reports.
    retires: Optional[date] = None


def _spec(provider, api_model, label, tier, best_for, price_in, price_out, supports_effort=True, active=True,
          retires=None):
    return ModelSpec(
        f"{provider}:{api_model}", provider, api_model, label, tier, best_for, price_in, price_out, supports_effort,
        active, retires,
    )


def _today() -> date:
    """Today in UTC; tests replace it."""
    return datetime.now(timezone.utc).date()


def _in_service(spec: ModelSpec, today: Optional[date] = None) -> bool:
    return spec.active and (spec.retires is None or (today or _today()) < spec.retires)


# Prices: Anthropic from the Claude API price table, OpenAI from OpenAI's pricing page (standard tier).
_SPECS: tuple[ModelSpec, ...] = (
    _spec("anthropic", "claude-sonnet-5-5", "Sonnet 5.5", "balanced",
          "Best value: precise template edits and solid prose", 2, 10),
    _spec("anthropic", "claude-opus-5-5", "Opus 5.5", "premium", "Top quality writing and judgment", 4, 20),
    # Price for prompts up to 100K tokens (a resume prompt is far below that).
    _spec("anthropic", "claude-haiku-5-5", "Haiku 5.5", "budget",
          "Cheapest Claude; built for high-volume tailoring", 0.1, 0.5),
    _spec("anthropic", "claude-haiku-4-5", "Haiku 4.5", "budget",
          "Older Haiku; ten times the price of Haiku 5.5", 1, 5, supports_effort=False),
    _spec("anthropic", "claude-fable-5-1", "Fable 5.1", "premium", "Maximum quality, most expensive", 10, 50),
    _spec("anthropic", "claude-sonnet-5", "Sonnet 5", "balanced",
          "Previous-generation Sonnet at the same price; a fallback", 2, 10),
    _spec("anthropic", "claude-opus-5", "Opus 5", "premium",
          "Previous-generation Opus; for comparison or as a fallback", 5, 25),
    _spec("anthropic", "claude-opus-4-8", "Opus 4.8", "premium",
          "Older Opus; for comparison or as a fallback", 5, 25),
    _spec("openai", "gpt-6.1-sol", "GPT-6.1 Sol", "balanced",
          "Near-Astra quality at a fraction of the cost", 2, 10, active=False),
    _spec("openai", "gpt-6-astra", "GPT-6 Astra", "premium", "OpenAI's most capable model", 10, 50),
    _spec("openai", "gpt-5.6-sol", "GPT-5.6 Sol", "premium", "Older flagship; for comparison", 4, 20),
    _spec("openai", "gpt-5.5", "GPT-5.5", "premium", "Older flagship; for comparison", 5, 30),
    _spec("openai", "gpt-5.6-terra", "GPT-5.6 Terra", "balanced", "Older mid-tier model", 2, 12),
    _spec("openai", "gpt-5.4-mini", "GPT-5.4 mini", "budget", "Good quality at a low price", 0.75, 4.5),
    _spec("openai", "gpt-5-mini", "GPT-5 mini", "budget", "Low cost; proven for resumes here", 0.25, 2),
    _spec("openai", "gpt-4o-mini", "GPT-4o mini", "budget",
          "Older non-reasoning mini; fast and cheap", 0.15, 0.6, supports_effort=False, retires=date(2027, 1, 14)),
    _spec("openai", "gpt-6-luna", "GPT-6 Luna", "budget",
          "Built for focused, high-volume work; cheapest", 0.1, 0.5),
)
_BY_ID = {s.id: s for s in _SPECS}

# Provider prefixes that were removed (NVIDIA "free:" presets). Old jobs may still carry such an id.
_RETIRED_PREFIXES = ("free:",)

_A = "anthropic:claude-"
_O = "openai:"

# Ordered presets per (task, provider): (model id, short tag shown in the picker). First active one = recommended.
PRESETS: dict[tuple[Task, Provider], list[tuple[str, str]]] = {
    ("resume", "anthropic"): [
        (_A + "haiku-5-5", "Cheapest"), (_A + "sonnet-5-5", "Best value"), (_A + "opus-5-5", "Top quality"),
        (_A + "haiku-4-5", "Older generation"), (_A + "sonnet-5", "Older generation"),
    ],
    ("cover_letter", "anthropic"): [
        (_A + "opus-5-5", "Recommended"), (_A + "fable-5-1", "Max quality"), (_A + "sonnet-5-5", "Lower cost"),
        (_A + "opus-5", "Older generation"), (_A + "opus-4-8", "Older generation"),
    ],
    ("screening", "anthropic"): [
        (_A + "sonnet-5-5", "Best value"), (_A + "opus-5-5", "Top quality"),
        (_A + "haiku-4-5", "Cheapest"), (_A + "sonnet-5", "Older generation"),
    ],
    # GPT-6.1 Sol is off; the entry after it is the recommendation meanwhile.
    ("resume", "openai"): [
        (_O + "gpt-6.1-sol", "Recommended"), (_O + "gpt-5-mini", "Low cost"), (_O + "gpt-5.4-mini", "Balanced"),
        (_O + "gpt-4o-mini", "Older generation"), (_O + "gpt-6-luna", "Cheapest"),
    ],
    ("cover_letter", "openai"): [
        (_O + "gpt-6.1-sol", "Recommended"), (_O + "gpt-5.6-sol", "Older flagship"),
        (_O + "gpt-6-astra", "Max quality"), (_O + "gpt-5.5", "Older flagship"),
        (_O + "gpt-5.6-terra", "Older mid-tier"),
    ],
    ("screening", "openai"): [
        (_O + "gpt-6.1-sol", "Recommended"), (_O + "gpt-5.4-mini", "Balanced"),
        (_O + "gpt-6-astra", "Max quality"), (_O + "gpt-5-mini", "Low cost"), (_O + "gpt-6-luna", "Cheapest"),
    ],
    # Judge (resume prompt evaluator): scores a generated resume against a JD in a fresh context. Strong
    # models only; it should be at least as capable as the model that wrote the resume.
    ("judge", "anthropic"): [
        (_A + "opus-5-5", "Recommended"), (_A + "fable-5-1", "Max quality"), (_A + "sonnet-5-5", "Lower cost"),
    ],
    ("judge", "openai"): [
        (_O + "gpt-6-astra", "Max quality"), (_O + "gpt-5.6-sol", "Older flagship"), (_O + "gpt-5.5", "Older flagship"),
    ],
}

# Job Applies tailors in bulk: budget models only. GPT-4o mini is the default through 2026 (the resume
# prompts were tuned on it); GPT-6 Luna from 2027, before GPT-4o mini's retirement. Haiku 5.5 is offered as a
# choice at Luna's price (user, 2026-11-20), never as the default. OpenAI: those two only (user, 2026-11-25).
JOB_APPLIES_RESUME_MODELS: tuple[str, ...] = (_O + "gpt-6-luna", _A + "haiku-5-5", _O + "gpt-4o-mini")
_JOB_APPLIES_LUNA_FROM = date(2027, 1, 1)


def _provider_configured(provider: Provider) -> bool:
    settings = get_settings()
    key = settings.ANTHROPIC_API_KEY if provider == "anthropic" else settings.OPENAI_API_KEY
    return bool((key or "").strip())


def _preset_ids(task: Task, provider: Provider) -> list[str]:
    return [model_id for model_id, _tag in PRESETS[(task, provider)] if _in_service(_BY_ID[model_id])]


def _is_retired(model_id: str) -> bool:
    return model_id.startswith(_RETIRED_PREFIXES)


def is_inactive(model_id: Optional[str]) -> bool:
    """A known model that has been switched off, or one from a removed provider."""
    wanted = (model_id or "").strip()
    spec = _BY_ID.get(wanted)
    return (spec is not None and not _in_service(spec)) or _is_retired(wanted)


def preset_tag(task: Task, model_id: str) -> Optional[str]:
    for provider in PROVIDER_ORDER:
        for preset_id, tag in PRESETS[(task, provider)]:
            if preset_id == model_id:
                return tag
    return None


def available_models(task: Task, user: Optional[UserExtra] = None) -> list[ModelSpec]:
    """Presets for `task` whose provider is configured, Anthropic first, in preset order."""
    return [
        _BY_ID[model_id]
        for provider in PROVIDER_ORDER
        if _provider_configured(provider)
        for model_id in _preset_ids(task, provider)
    ]


def get_model(model_id: str, user: Optional[UserExtra] = None) -> Optional[ModelSpec]:
    """Any known model whose provider is usable right now (regardless of task)."""
    spec = _BY_ID.get(model_id)
    return spec if spec is not None and _provider_configured(spec.provider) else None


_DEFAULT_SETTING: dict[Task, str] = {
    "resume": "RESUME_DEFAULT_MODEL",
    "cover_letter": "COVER_LETTER_DEFAULT_MODEL",
    "screening": "SCREENING_DEFAULT_MODEL",
    "judge": "JUDGE_DEFAULT_MODEL",
}


def _configured_default(task: Task) -> str:
    # Looked up by name so only the asked-for setting has to exist (tests stub settings per task).
    return getattr(get_settings(), _DEFAULT_SETTING[task])


def _openai_recommendation(task: Task) -> str:
    return _preset_ids(task, "openai")[0]


def default_model_id(task: Task, user: Optional[UserExtra] = None) -> Optional[str]:
    usable = [s.id for s in available_models(task)]
    configured = _configured_default(task)
    if configured in usable:
        return configured
    return usable[0] if usable else None


def validate_requested(task: Task, model_id: Optional[str], user: Optional[UserExtra] = None) -> Optional[ModelSpec]:
    wanted = (model_id or "").strip()
    if not wanted:
        return None
    spec = _BY_ID.get(wanted)
    if spec is None and _is_retired(wanted):
        raise ModelNotAvailable(f"Model '{wanted}' has been removed; pick another model.")
    if spec is None:
        raise ModelNotAvailable(f"Unknown model '{wanted}'.")
    if not spec.active:
        raise ModelNotAvailable(f"{spec.label} is turned off; pick another model.")
    if not _in_service(spec):
        raise ModelNotAvailable(f"{spec.label} was retired by {PROVIDER_LABEL[spec.provider]}; pick another model.")
    if wanted not in _preset_ids(task, spec.provider):
        raise ModelNotAvailable(f"Model '{wanted}' can't be used for {task.replace('_', ' ')}.")
    if not _provider_configured(spec.provider):
        raise ModelNotAvailable(f"Model '{wanted}' is not configured on this server.")
    return spec


def job_applies_resume_models(today: Optional[date] = None) -> list[ModelSpec]:
    """The resume models Job Applies offers: still in service, provider configured, cheapest first."""
    return [
        spec for spec in (_BY_ID[i] for i in JOB_APPLIES_RESUME_MODELS)
        if _in_service(spec, today) and _provider_configured(spec.provider)
    ]


def job_applies_resume_default(today: Optional[date] = None) -> str:
    """GPT-4o mini through 2026, GPT-6 Luna from 2027-01-01 (whether or not OpenAI is configured: the runner falls
    back as for any request)."""
    return _O + ("gpt-4o-mini" if (today or _today()) < _JOB_APPLIES_LUNA_FROM else "gpt-6-luna")


def fallback_chain(task: Task, requested_id: Optional[str], user: Optional[UserExtra] = None) -> list[ModelSpec]:
    """Preferred model, then the task default, then OpenAI's recommendation; usable ones only."""
    usable = {s.id: s for s in available_models(task)}
    wanted = (requested_id or "").strip() or default_model_id(task)
    chain: list[ModelSpec] = []
    for model_id in (wanted, _configured_default(task), _openai_recommendation(task)):
        spec = usable.get(model_id or "")
        if spec is not None and spec not in chain:
            chain.append(spec)
    return chain


def cache_lane(spec: ModelSpec, task: Task, user: Optional[UserExtra] = None) -> str:
    """Key part for the resume LLM output cache: the model plus the reasoning effort it ran with."""
    effort = TASK_EFFORT[task] if spec.supports_effort else "none"
    return f"{spec.provider}:{spec.api_model}:{effort}"


def usage_cost(spec: ModelSpec, usage: dict) -> float:
    return (
        (usage.get("input") or 0) * spec.price_in + (usage.get("output") or 0) * spec.price_out
    ) / 1_000_000


_BY_API_MODEL = {s.api_model: s for s in _SPECS}


def spec_for_api_model(api_model: Optional[str]) -> Optional[ModelSpec]:
    """The catalog entry for a bare provider model name (e.g. "gpt-4o-mini"), configured or not."""
    return _BY_API_MODEL.get((api_model or "").strip())


def api_model_cost(api_model: Optional[str], usage: dict) -> Optional[float]:
    """USD for `usage` on a model named by its API name; None when the catalog has no price for it."""
    spec = spec_for_api_model(api_model)
    return usage_cost(spec, usage) if spec is not None else None


def estimate_call_cost(spec: ModelSpec, task: Task) -> float:
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
        "recommended": _preset_ids(task, spec.provider)[0] == spec.id,
        "tag": preset_tag(task, spec.id) or "",
    }
