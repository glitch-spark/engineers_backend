from __future__ import annotations

from typing import Optional

from beanie import PydanticObjectId

from app.config import get_settings
from app.llm import rate_limiter
from app.llm.settings import (
    decrypted_nvidia_key,
    decrypted_openai_key,
    get_or_create_llm_settings,
    openai_endpoint_from_settings,
)
from app.llm.types import (
    PROVIDER_NVIDIA_FREE,
    PROVIDER_OPENAI,
    EndpointConfig,
    LlmProvider,
    ResolvedLlmConfig,
)
from app.models import Account, UserExtra


def resolve_provider_preference(
    user: UserExtra,
    account: Optional[Account] = None,
) -> LlmProvider:
    if account is not None and account.llmProvider is not None:
        return account.llmProvider
    return user.llmProvider or PROVIDER_OPENAI


def _is_allowlisted(
    user_id: PydanticObjectId,
    account_id: Optional[PydanticObjectId],
    user_ids: list,
    account_ids: list,
) -> bool:
    if user_id in user_ids:
        return True
    if account_id is not None and account_id in account_ids:
        return True
    return False


def _openai_endpoint(doc, api_key: str, model: str) -> EndpointConfig:
    env = get_settings()
    return EndpointConfig(
        provider=PROVIDER_OPENAI,
        api_key=api_key,
        base_url=doc.openaiBaseUrl,
        model=model or doc.openaiResumeModel or env.RESUME_OPENAI_MODEL,
        reasoning_effort=env.RESUME_REASONING_EFFORT,
    )


def _nvidia_endpoint(doc, api_key: str) -> EndpointConfig:
    return EndpointConfig(
        provider=PROVIDER_NVIDIA_FREE,
        api_key=api_key,
        base_url=doc.nvidiaBaseUrl,
        model=doc.nvidiaResumeModel,
        temperature=doc.nvidiaTemperature,
        top_p=doc.nvidiaTopP,
        max_tokens=doc.nvidiaMaxTokens,
        extra_body={"chat_template_kwargs": {"thinking": False}},
    )


async def resolve_llm_config(
    user: UserExtra,
    account: Optional[Account] = None,
) -> ResolvedLlmConfig:
    doc = await get_or_create_llm_settings()
    requested = resolve_provider_preference(user, account)

    openai_key = decrypted_openai_key(doc)
    if not openai_key:
        raise RuntimeError("OpenAI API key not configured")

    backup = _openai_endpoint(doc, openai_key, doc.openaiResumeModel)

    effective = requested
    if requested == PROVIDER_NVIDIA_FREE:
        nvidia_key = decrypted_nvidia_key(doc)
        allowlisted = _is_allowlisted(
            user.id,
            account.id if account is not None else None,
            doc.freeTierUserIds,
            doc.freeTierAccountIds,
        )
        if not nvidia_key or not allowlisted:
            effective = PROVIDER_OPENAI
        elif rate_limiter.is_exhausted(doc.nvidiaMaxRpm):
            effective = PROVIDER_OPENAI
        else:
            primary = _nvidia_endpoint(doc, nvidia_key)
            return ResolvedLlmConfig(
                primary=primary,
                backup=backup,
                requested_provider=requested,
                effective_provider=effective,
            )

    return ResolvedLlmConfig(
        primary=backup,
        backup=backup,
        requested_provider=requested,
        effective_provider=PROVIDER_OPENAI,
    )
