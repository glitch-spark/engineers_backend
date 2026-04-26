from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.deps import get_current_user, require_admin
from app.models import GlobalPrompt, UserExtra
from app.routers._util import doc_to_dict

router = APIRouter(prefix="/global-prompt", tags=["global-prompt"])

DEFAULT_KEY = "default"


class GlobalPromptIn(BaseModel):
    systemPrompt: str = Field(default="", max_length=20000)


async def _get_or_create_default() -> GlobalPrompt:
    doc = await GlobalPrompt.find_one(GlobalPrompt.key == DEFAULT_KEY)
    if doc is None:
        doc = GlobalPrompt(key=DEFAULT_KEY, systemPrompt="")
        await doc.insert()
    return doc


@router.get("")
async def get_global_prompt(
    _: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    doc = await _get_or_create_default()
    return doc_to_dict(doc)


@router.put("")
async def update_global_prompt(
    body: GlobalPromptIn,
    _: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    doc = await _get_or_create_default()
    doc.systemPrompt = body.systemPrompt
    await doc.save()
    return doc_to_dict(doc)
