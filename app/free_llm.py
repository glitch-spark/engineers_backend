"""NVIDIA Integrate free-tier LLM presets for resume generation and screening.

Users pick a preset on the Profile page and supply their own nvapi-* key.
Keys are never hardcoded here — see llm/ references locally for testing only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# OpenAI SDK timeout (seconds) for NVIDIA Integrate calls.
FREE_LLM_TEST_TIMEOUT_SECONDS = 45
FREE_LLM_SCREENING_TIMEOUT_SECONDS = 120
FREE_LLM_RESUME_TIMEOUT_SECONDS = 480  # 8 minutes
# Default for generic calls (legacy alias).
FREE_LLM_REQUEST_TIMEOUT_SECONDS = FREE_LLM_RESUME_TIMEOUT_SECONDS

# Cap output tokens on free resume calls (Profile max is 16384).
FREE_LLM_RESUME_MAX_OUTPUT_TOKENS = 16000

MIN_MAX_TOKENS = 512
MAX_MAX_TOKENS = 16384


@dataclass(frozen=True)
class FreeLlmPreset:
    id: str
    label: str
    model: str
    default_max_tokens: int
    extra_body: Optional[dict[str, Any]] = None
    temperature: float = 1.0
    top_p: float = 0.95


FREE_LLM_PRESETS: tuple[FreeLlmPreset, ...] = (
    FreeLlmPreset(
        id="deepseek-v4-pro",
        label="DeepSeek V4 Pro",
        model="deepseek-ai/deepseek-v4-pro",
        default_max_tokens=16384,
        extra_body={"chat_template_kwargs": {"thinking": False}},
    ),
    FreeLlmPreset(
        id="minimax-m3",
        label="MiniMax M3",
        model="minimaxai/minimax-m3",
        default_max_tokens=8192,
    ),
    FreeLlmPreset(
        id="minimax-m2.7",
        label="MiniMax M2.7",
        model="minimaxai/minimax-m2.7",
        default_max_tokens=8192,
    ),
    FreeLlmPreset(
        id="glm-5.2",
        label="GLM 5.2",
        model="z-ai/glm-5.2",
        default_max_tokens=16384,
        top_p=1.0,
    ),
    FreeLlmPreset(
        id="glm-5.3",
        label="GLM 5.3",
        model="z-ai/glm-5.3",
        default_max_tokens=16384,
        top_p=1.0,
    ),
    FreeLlmPreset(
        id="kimi-k3",
        label="Kimi K3",
        model="moonshotai/kimi-k3",
        default_max_tokens=16384,
        extra_body={"reasoning_effort": "low"},
    ),
)

_PRESET_BY_ID = {p.id: p for p in FREE_LLM_PRESETS}


def get_preset(preset_id: Optional[str]) -> Optional[FreeLlmPreset]:
    if not preset_id:
        return None
    return _PRESET_BY_ID.get(preset_id.strip())


def preset_catalog_public() -> list[dict[str, Any]]:
    """Safe for API — no secrets."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "model": p.model,
            "defaultMaxTokens": p.default_max_tokens,
        }
        for p in FREE_LLM_PRESETS
    ]


def clamp_max_tokens(value: Optional[int], preset: Optional[FreeLlmPreset]) -> int:
    default = preset.default_max_tokens if preset else 8192
    if value is None:
        return default
    return max(MIN_MAX_TOKENS, min(MAX_MAX_TOKENS, int(value)))


def api_key_hint(key: Optional[str]) -> str:
    if not key or len(key) < 4:
        return ""
    return f"••••{key[-4:]}"
