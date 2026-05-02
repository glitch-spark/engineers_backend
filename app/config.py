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
    OPENAI_MODEL: str = "gpt-4o-mini"
    RESUME_OPENAI_MODEL: str = "gpt-5-mini"
    AWS_REGION: Optional[str] = None
    AWS_S3_BUCKET: Optional[str] = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
