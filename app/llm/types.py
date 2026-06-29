from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

LlmProvider = Literal["openai", "nvidia_free"]

PROVIDER_OPENAI: LlmProvider = "openai"
PROVIDER_NVIDIA_FREE: LlmProvider = "nvidia_free"

PROVIDER_OPTIONS = [
    {
        "id": PROVIDER_OPENAI,
        "label": "OpenAI (default)",
        "description": "Paid, higher reliability",
    },
    {
        "id": PROVIDER_NVIDIA_FREE,
        "label": "Free AI — DeepSeek v4",
        "description": "Shared 40 req/min; falls back to OpenAI if busy",
    },
]


@dataclass
class EndpointConfig:
    provider: LlmProvider
    api_key: str
    model: str
    base_url: Optional[str] = None
    temperature: float = 0.6
    top_p: float = 0.95
    max_tokens: int = 16384
    reasoning_effort: str = "low"
    extra_body: Optional[dict] = None


@dataclass
class LlmCompletionResult:
    text: str
    usage: dict = field(default_factory=dict)
    provider_used: LlmProvider = PROVIDER_OPENAI
    model_used: str = ""
    fallback_used: bool = False
    fallback_reason: Optional[str] = None


@dataclass
class ResolvedLlmConfig:
    primary: EndpointConfig
    backup: EndpointConfig
    requested_provider: LlmProvider
    effective_provider: LlmProvider
