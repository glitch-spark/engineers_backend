from functools import lru_cache
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    MONGODB_URI: str
    AUTH_SECRET: str
    FRONTEND_ORIGIN: str = "http://localhost:5173"
    APP_BASE_URL: str = "http://localhost:8080"
    APP_TIMEZONE: str = "America/New_York"
    SLACK_WEBHOOK_URL: Optional[str] = None
    # Slack interview bot (private DMs). Bot token sends messages; OAuth
    # client identifies which Slack user connected. Cron secret protects the
    # scheduled tick endpoint. All optional so the app boots without Slack.
    SLACK_BOT_TOKEN: Optional[str] = None
    SLACK_CLIENT_ID: Optional[str] = None
    SLACK_CLIENT_SECRET: Optional[str] = None
    SLACK_OAUTH_REDIRECT_URI: str = "http://localhost:8080/integrations/slack/oauth-callback"
    SLACK_BOT_CRON_SECRET: Optional[str] = None
    SLACK_DIGEST_HOUR: int = 8
    SLACK_DIGEST_MINUTE: int = 0
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
    # input tokens with negligible quality loss. ~12000 chars ≈ 3K tokens.
    RESUME_JD_MAX_CHARS: int = 12000
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
    # Gmail integration — OAuth client + Fernet key for refresh-token storage.
    # If GOOGLE_OAUTH_CLIENT_ID is unset, the gmail router 503s on use so the
    # rest of the app still boots without these. EMAIL_TOKEN_KEY is a 32-byte
    # urlsafe-base64 Fernet key — generate one with
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None
    GOOGLE_OAUTH_CLIENT_SECRET: Optional[str] = None
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8080/integrations/gmail/oauth-callback"
    EMAIL_TOKEN_KEY: Optional[str] = None
    # Days of inbox history pulled on the first connect. Subsequent syncs use
    # Gmail's history.list delta API and only fetch new messages. 30d captures
    # recent job-search activity; capped by EMAIL_SYNC_MAX_MESSAGES.
    EMAIL_INITIAL_SYNC_DAYS: int = 30
    # Hard cap on messages classified per sync — guards LLM cost on noisy
    # inboxes. Anything older than the cap is skipped (not failed).
    EMAIL_SYNC_MAX_MESSAGES: int = 500
    # Min classifier confidence (0-1) required to auto-advance an Application
    # stage. Below threshold the email lands in the Needs Review tray.
    EMAIL_AUTO_ADVANCE_CONFIDENCE: float = 0.8
    # Microsoft Identity OAuth for the Outlook integration. Same shape as
    # the Google block — if MS_OAUTH_CLIENT_ID is unset the outlook routes
    # 503 on use so the rest of the app still boots.
    # MS_OAUTH_TENANT is the Azure AD tenant id; "common" supports both
    # personal and work accounts. Pin to your tenant id once you submit
    # the app for org verification.
    MS_OAUTH_CLIENT_ID: Optional[str] = None
    MS_OAUTH_CLIENT_SECRET: Optional[str] = None
    MS_OAUTH_REDIRECT_URI: str = "http://localhost:8080/integrations/outlook/oauth-callback"
    MS_OAUTH_TENANT: str = "common"
    AWS_REGION: Optional[str] = None
    AWS_S3_BUCKET: Optional[str] = None
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None

    @field_validator("SLACK_WEBHOOK_URL", mode="before")
    @classmethod
    def _normalize_slack_webhook(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Clear cached settings (e.g. after .env change in dev)."""
    get_settings.cache_clear()
    return get_settings()
