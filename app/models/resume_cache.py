from typing import Optional

from pymongo import IndexModel

from app.models.base import TimestampedDocument


class ResumeLLMCache(TimestampedDocument):
    """Caches the raw LLM output for one resume generation, keyed by a content
    hash of (model, reasoning effort, full prompt). A hash collision-free key
    means identical regenerations — same template + job description + custom
    prompt + model — reuse the stored output and skip the OpenAI call entirely.
    PDF rendering still runs locally on a hit; only the API call is avoided.

    The template lives inside the hashed prompt, so editing a profile's
    template changes the key and naturally misses stale entries. Entries are
    never updated, only inserted; growth is unbounded for now (add a TTL index
    if it becomes a concern)."""

    promptHash: str
    output: str
    model: str
    provider: str = "openai"
    inputTokens: Optional[int] = None
    outputTokens: Optional[int] = None
    reasoningTokens: Optional[int] = None

    class Settings:
        name = "resume_llm_cache"
        indexes = [
            IndexModel([("promptHash", 1)], unique=True),
        ]
