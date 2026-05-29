from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    MONGODB_URI: str
    AUTH_SECRET: str
    FRONTEND_ORIGIN: str = "http://localhost:5173"
    APP_BASE_URL: str = "http://localhost:8000"
    APP_TIMEZONE: str = "America/New_York"
    SLACK_WEBHOOK_URL: Optional[str] = None
    JWT_EXPIRES_MIN: int = 720
    OPENAI_API_KEY: Optional[str] = None
    # Streamed AI Review (interview): chat-completions, long-form. Mini is enough.
    OPENAI_MODEL: str = "gpt-4o-mini"
    # High-quality structured generation: resume HTML, screening answers, scoring.
    # Use a smarter model — output ships to users and judgment matters.
    RESUME_OPENAI_MODEL: str = "gpt-5-mini"
    # Reasoning effort for the resume rewrite (gpt-5* only). Mechanical
    # text-replace needs little reasoning — "low" cuts hidden reasoning
    # tokens (billed at output rate) ~5-10x vs the medium default.
    # Values: minimal | low | medium | high.
    RESUME_REASONING_EFFORT: str = "low"
    # Soft cap on JD chars fed to the resume LLM. Core requirements sit at
    # the top; the tail is benefits/EEO/legal boilerplate. Trimming cuts
    # input tokens with negligible quality loss. ~8000 chars ≈ 2K tokens.
    RESUME_JD_MAX_CHARS: int = 8000
    # Reasoning effort for non-resume gpt-5* calls (interview rollup,
    # follow-up chat). Same rationale as RESUME_REASONING_EFFORT: the
    # medium default bills heavy hidden reasoning tokens. low is plenty for
    # these synthesis/chat tasks. minimal | low | medium | high.
    AI_REASONING_EFFORT: str = "low"
    # Output-token ceilings to stop runaway generations (billed at output
    # rate). Tunable per deployment.
    AI_CHAT_MAX_TOKENS: int = 800
    AI_REVIEW_MAX_TOKENS: int = 1500
    # Cheap structured-extraction tasks: parsing Q&A from transcripts, rollup tips.
    # No real reasoning, just JSON shape. Mini saves ~50% per call.
    EXTRACTION_MODEL: str = "gpt-4o-mini"
    AWS_REGION: Optional[str] = None
    AWS_S3_BUCKET: Optional[str] = None
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
