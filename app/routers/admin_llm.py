from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.deps import get_current_user, require_admin
from app.llm.crypto import encrypt_secret, mask_secret
from app.llm.settings import get_or_create_llm_settings
from app.llm.types import PROVIDER_OPTIONS
from app.models import UserExtra
from app.routers._util import doc_to_dict, to_object_id

router = APIRouter(tags=["llm-admin"])


class LlmSettingsOut(BaseModel):
    openaiApiKeyMasked: str = ""
    nvidiaApiKeyMasked: str = ""
    openaiBaseUrl: Optional[str] = None
    nvidiaBaseUrl: str = ""
    openaiResumeModel: str = ""
    nvidiaResumeModel: str = ""
    nvidiaMaxRpm: int = 40
    nvidiaTemperature: float = 1.0
    nvidiaTopP: float = 0.95
    nvidiaMaxTokens: int = 16384
    freeTierUserIds: List[str] = []
    freeTierAccountIds: List[str] = []


class LlmSettingsIn(BaseModel):
    openaiApiKey: Optional[str] = None
    nvidiaApiKey: Optional[str] = None
    openaiBaseUrl: Optional[str] = None
    nvidiaBaseUrl: Optional[str] = None
    openaiResumeModel: Optional[str] = None
    nvidiaResumeModel: Optional[str] = None
    nvidiaMaxRpm: Optional[int] = Field(None, ge=1, le=1000)
    nvidiaTemperature: Optional[float] = Field(None, ge=0, le=2)
    nvidiaTopP: Optional[float] = Field(None, ge=0, le=1)
    nvidiaMaxTokens: Optional[int] = Field(None, ge=256, le=128000)
    freeTierUserIds: Optional[List[str]] = None
    freeTierAccountIds: Optional[List[str]] = None


def _to_out(doc) -> LlmSettingsOut:
    return LlmSettingsOut(
        openaiApiKeyMasked=mask_secret(doc.openaiApiKey or ""),
        nvidiaApiKeyMasked=mask_secret(doc.nvidiaApiKey or ""),
        openaiBaseUrl=doc.openaiBaseUrl,
        nvidiaBaseUrl=doc.nvidiaBaseUrl,
        openaiResumeModel=doc.openaiResumeModel,
        nvidiaResumeModel=doc.nvidiaResumeModel,
        nvidiaMaxRpm=doc.nvidiaMaxRpm,
        nvidiaTemperature=doc.nvidiaTemperature,
        nvidiaTopP=doc.nvidiaTopP,
        nvidiaMaxTokens=doc.nvidiaMaxTokens,
        freeTierUserIds=[str(x) for x in doc.freeTierUserIds],
        freeTierAccountIds=[str(x) for x in doc.freeTierAccountIds],
    )


@router.get("/llm/providers")
async def list_providers(
    _: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    return {"providers": PROVIDER_OPTIONS}


@router.get("/admin/llm-settings")
async def get_llm_settings(
    _: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    doc = await get_or_create_llm_settings()
    return {"settings": _to_out(doc).model_dump()}


@router.put("/admin/llm-settings")
async def update_llm_settings(
    body: LlmSettingsIn,
    _: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    from app.config import get_settings

    doc = await get_or_create_llm_settings()
    secret = get_settings().AUTH_SECRET

    if body.openaiApiKey is not None and body.openaiApiKey.strip():
        doc.openaiApiKey = encrypt_secret(body.openaiApiKey.strip(), secret)
    if body.nvidiaApiKey is not None and body.nvidiaApiKey.strip():
        doc.nvidiaApiKey = encrypt_secret(body.nvidiaApiKey.strip(), secret)
    if body.openaiBaseUrl is not None:
        doc.openaiBaseUrl = body.openaiBaseUrl.strip() or None
    if body.nvidiaBaseUrl is not None:
        doc.nvidiaBaseUrl = body.nvidiaBaseUrl.strip() or doc.nvidiaBaseUrl
    if body.openaiResumeModel is not None:
        doc.openaiResumeModel = body.openaiResumeModel.strip() or doc.openaiResumeModel
    if body.nvidiaResumeModel is not None:
        doc.nvidiaResumeModel = body.nvidiaResumeModel.strip() or doc.nvidiaResumeModel
    if body.nvidiaMaxRpm is not None:
        doc.nvidiaMaxRpm = body.nvidiaMaxRpm
    if body.nvidiaTemperature is not None:
        doc.nvidiaTemperature = body.nvidiaTemperature
    if body.nvidiaTopP is not None:
        doc.nvidiaTopP = body.nvidiaTopP
    if body.nvidiaMaxTokens is not None:
        doc.nvidiaMaxTokens = body.nvidiaMaxTokens
    if body.freeTierUserIds is not None:
        doc.freeTierUserIds = [to_object_id(x) for x in body.freeTierUserIds]
    if body.freeTierAccountIds is not None:
        doc.freeTierAccountIds = [to_object_id(x) for x in body.freeTierAccountIds]

    await doc.save()
    return {"settings": _to_out(doc).model_dump()}
