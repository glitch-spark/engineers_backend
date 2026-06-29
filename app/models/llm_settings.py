from typing import List, Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument

DEFAULT_KEY = "default"


class LlmSettings(TimestampedDocument):
    """Singleton admin-managed LLM credentials and free-tier allowlist."""

    key: str = DEFAULT_KEY
    openaiApiKey: str = ""  # Fernet-encrypted at rest
    nvidiaApiKey: str = ""
    openaiBaseUrl: Optional[str] = None
    nvidiaBaseUrl: str = "https://integrate.api.nvidia.com/v1"
    openaiResumeModel: str = "gpt-5-mini"
    nvidiaResumeModel: str = "deepseek-ai/deepseek-v4-pro"
    nvidiaMaxRpm: int = 40
    nvidiaTemperature: float = 1.0
    nvidiaTopP: float = 0.95
    nvidiaMaxTokens: int = 16384
    freeTierUserIds: List[PydanticObjectId] = []
    freeTierAccountIds: List[PydanticObjectId] = []

    class Settings:
        name = "llm_settings"
        indexes = [
            IndexModel([("key", 1)], unique=True),
        ]
