import re
from datetime import date
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


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
    SLACK_CALLER_CHANNEL_ID: Optional[str] = None
    # Daily list of the day's confirmed caller interviews (e.g. #engineer-caller-test); unset = off.
    # Posted once a day at SLACK_CALLER_DAILY_TIME (HH:MM) in SLACK_CALLER_DAILY_TIMEZONE.
    SLACK_CALLER_DAILY_CHANNEL_ID: Optional[str] = None
    SLACK_CALLER_DAILY_TIME: str = "05:00"
    SLACK_CALLER_DAILY_TIMEZONE: str = "America/Chicago"
    # Channel (C…) where the bot posts each daily plan and edits it in place.
    SLACK_DAILY_PLAN_CHANNEL_ID: Optional[str] = None
    # Channel (C…) where the bot posts each transaction, with Approve/Reject buttons. Unset = no posts.
    SLACK_TRANSACTIONS_CHANNEL_ID: Optional[str] = None
    # Verifies Slack's signature on button clicks (Slack app → Basic Information). Unset = no buttons.
    SLACK_SIGNING_SECRET: Optional[str] = None
    JWT_EXPIRES_MIN: int = 720
    OPENAI_API_KEY: Optional[str] = None
    # Streamed AI Review (interview): chat-completions, long-form. Mini is enough.
    OPENAI_MODEL: str = "gpt-4o-mini"
    # High-quality structured generation: resume HTML, screening answers, scoring.
    # Use a smarter model — output ships to users and judgment matters.
    RESUME_OPENAI_MODEL: str = "gpt-5-mini"
    # Anthropic key for the selectable Claude models (app/model_catalog.py). Unset hides them.
    ANTHROPIC_API_KEY: Optional[str] = None
    # Only for user-level Anthropic keys (sk-ant-usr-...): the workspace every request is billed to.
    ANTHROPIC_WORKSPACE_ID: Optional[str] = None
    # Default model per task when the caller doesn't pick one (provider-prefixed ids).
    RESUME_DEFAULT_MODEL: str = "anthropic:claude-sonnet-5-5"
    COVER_LETTER_DEFAULT_MODEL: str = "anthropic:claude-opus-5-5"
    SCREENING_DEFAULT_MODEL: str = "anthropic:claude-sonnet-5-5"
    # Reasoning effort for the resume rewrite (gpt-5* only). Mechanical
    # text-replace needs little reasoning — "low" cuts hidden reasoning
    # tokens (billed at output rate) ~5-10x vs the medium default.
    # Values: minimal | low | medium | high.
    RESUME_REASONING_EFFORT: str = "low"
    # Soft cap on JD chars fed to the resume LLM. Core requirements sit at
    # the top; the tail is benefits/EEO/legal boilerplate. Trimming cuts
    # input tokens with negligible quality loss. ~12000 chars ≈ 3K tokens.
    RESUME_JD_MAX_CHARS: int = 12000
    # Generation queue (app/resume_queue.py): resumes generating at once, PDF renders (Chromium) at once,
    # and generations a user may queue per day (both the Generator page and Job Applies).
    RESUME_LLM_CONCURRENCY: int = 3
    RESUME_RENDER_CONCURRENCY: int = 2
    RESUME_DAILY_CAP: int = 300
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
    # Job Applies: OpenAI model that reads job postings (locations, work mode, clearance). Empty = RESUME_OPENAI_MODEL.
    JOB_EXTRACT_MODEL: str = ""
    # Service account for writing Job Applies exports into a Google Sheet shared with its email as Editor:
    # the key file's JSON (or a path to it). Unset = export disabled with a setup message.
    GOOGLE_SERVICE_ACCOUNT_JSON: Optional[str] = None
    AWS_REGION: Optional[str] = None
    AWS_S3_BUCKET: Optional[str] = None
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    # Bidders — screenshot folders are counted in object storage (only "s3" for now). Without an
    # endpoint (AWS S3) the access key / secret / region fall back to the AWS_* values above when
    # unset; with BID_STORAGE_ENDPOINT_URL set there is no AWS fallback (both BID_STORAGE_* keys needed).
    BID_STORAGE_PROVIDER: str = "s3"
    BID_STORAGE_ACCESS_KEY_ID: Optional[str] = None
    BID_STORAGE_SECRET_ACCESS_KEY: Optional[str] = None
    BID_STORAGE_REGION: Optional[str] = None
    # S3-compatible endpoint for non-AWS storage, e.g. Backblaze B2:
    # https://s3.us-east-005.backblazeb2.com (with the B2 key ID / application key above).
    # Unset = AWS S3.
    BID_STORAGE_ENDPOINT_URL: Optional[str] = None
    # Bucket for bidder screenshot folders (s3://<bucket>/<folder>/); blank = unset.
    BID_STORAGE_BUCKET: Optional[str] = None
    # Report schedule: wall-clock times in BID_REPORT_TIMEZONE (an IANA name). Daily report at
    # BID_DAILY_REPORT_TIME; weekly report on BID_WEEKLY_REPORT_DAY (monday..sunday) at
    # BID_WEEKLY_REPORT_TIME. Times are 24h HH:MM; invalid values fail at startup.
    BID_REPORT_TIMEZONE: str = "America/Chicago"
    BID_DAILY_REPORT_TIME: str = "18:30"
    BID_WEEKLY_REPORT_DAY: str = "saturday"
    BID_WEEKLY_REPORT_TIME: str = "16:00"
    # Reports, live counts and the Bidders page count approved bid records (the database). Optional
    # cut-over day (YYYY-MM-DD, in BID_REPORT_TIMEZONE): a window starting before that day's local
    # midnight (or straddling it) keeps the old screenshot-folder count. Unset = records everywhere.
    BID_RECORDS_START: Optional[str] = None
    # Slack channel the bid reports post to. Unset = reports are saved but not posted.
    SLACK_BIDS_CHANNEL_ID: Optional[str] = None

    @field_validator("SLACK_WEBHOOK_URL", mode="before")
    @classmethod
    def _normalize_slack_webhook(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("BID_STORAGE_ENDPOINT_URL", mode="before")
    @classmethod
    def _normalize_bid_endpoint(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip().rstrip("/")
        if not s:
            return None
        # Backblaze's console shows the endpoint without a scheme; botocore rejects that.
        return s if "://" in s else f"https://{s}"

    @field_validator("BID_STORAGE_BUCKET", mode="before")
    @classmethod
    def _normalize_bid_bucket(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        return str(v).strip() or None

    @field_validator("BID_RECORDS_START", mode="before")
    @classmethod
    def _validate_records_start(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        if not _YMD.match(s):
            raise ValueError("must be YYYY-MM-DD, e.g. 2026-10-06")
        try:
            date.fromisoformat(s)
        except ValueError as e:
            raise ValueError("not a calendar date, use YYYY-MM-DD") from e
        return s

    @field_validator("BID_DAILY_REPORT_TIME", "BID_WEEKLY_REPORT_TIME")
    @classmethod
    def _validate_report_time(cls, v: str) -> str:
        s = str(v).strip()
        if not _HHMM.match(s):
            raise ValueError("must be 24h HH:MM, e.g. 18:30")
        return s

    @field_validator("BID_WEEKLY_REPORT_DAY")
    @classmethod
    def _validate_report_day(cls, v: str) -> str:
        s = str(v).strip().lower()
        if s not in WEEKDAYS:
            raise ValueError("must be one of " + ", ".join(WEEKDAYS))
        return s

    @field_validator("BID_REPORT_TIMEZONE")
    @classmethod
    def _validate_report_timezone(cls, v: str) -> str:
        s = str(v).strip()
        try:
            ZoneInfo(s)
        except Exception as e:  # ZoneInfoNotFoundError, ValueError for malformed keys
            raise ValueError(f"unknown timezone {s!r}") from e
        return s


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Clear cached settings (e.g. after .env change in dev)."""
    get_settings.cache_clear()
    return get_settings()
