from __future__ import annotations

from typing import Optional

from app.config import get_settings
from app.llm.crypto import decrypt_secret
from app.models.llm_settings import DEFAULT_KEY, LlmSettings


async def get_or_create_llm_settings() -> LlmSettings:
    doc = await LlmSettings.find_one(LlmSettings.key == DEFAULT_KEY)
    if doc is None:
        env = get_settings()
        doc = LlmSettings(
            key=DEFAULT_KEY,
            openaiResumeModel=env.RESUME_OPENAI_MODEL,
        )
        await doc.insert()
    return doc


def _encryption_secret() -> str:
    env = get_settings()
    return env.AUTH_SECRET


def decrypted_openai_key(doc: LlmSettings) -> str:
    key = decrypt_secret(doc.openaiApiKey or "", _encryption_secret())
    if key:
        return key
    return get_settings().OPENAI_API_KEY or ""


def decrypted_nvidia_key(doc: LlmSettings) -> str:
    return decrypt_secret(doc.nvidiaApiKey or "", _encryption_secret())


def openai_endpoint_from_settings(doc: LlmSettings) -> Optional[dict]:
    api_key = decrypted_openai_key(doc)
    if not api_key:
        return None
    env = get_settings()
    return {
        "api_key": api_key,
        "base_url": doc.openaiBaseUrl,
        "model": doc.openaiResumeModel or env.RESUME_OPENAI_MODEL,
        "reasoning_effort": env.RESUME_REASONING_EFFORT,
    }
