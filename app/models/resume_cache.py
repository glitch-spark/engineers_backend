from typing import Optional

from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument

CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


class ResumeLLMCache(TimestampedDocument):
    """Caches the raw LLM output for one resume generation, keyed by a content
    hash of (lane + full prompt). A hash collision-free key means identical
    regenerations — same template + job description + custom prompt + model —
    reuse the stored output and skip the LLM call entirely.  PDF rendering
    still runs locally on a hit; only the API call is avoided.

    The template lives inside the hashed prompt, so editing a profile's
    template changes the key and naturally misses stale entries.

    `isFallback` marks entries produced by a fallback model after the
    requested one failed.

    Entries expire after CACHE_TTL_SECONDS (MongoDB TTL index on createdAt)
    so stale fallback entries are eventually cleaned up."""

    promptHash: str
    output: str
    model: str
    isFallback: bool = False
    inputTokens: Optional[int] = None
    outputTokens: Optional[int] = None
    reasoningTokens: Optional[int] = None

    class Settings:
        name = "resume_llm_cache"
        indexes = [
            IndexModel([("promptHash", ASCENDING)], unique=True),
            IndexModel(
                [("createdAt", ASCENDING)],
                expireAfterSeconds=CACHE_TTL_SECONDS,
            ),
        ]
