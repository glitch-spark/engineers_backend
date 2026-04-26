from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.deps import get_current_user, require_admin
from app.models import Skill, UserExtra
from app.routers._util import doc_to_dict, to_object_id

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    minInterviews: int = Field(ge=1)
    maxInterviews: int = Field(ge=1)
    systemPrompt: str = Field(default="", max_length=20000)

    @model_validator(mode="after")
    def _check_range(self) -> "SkillIn":
        if self.maxInterviews < self.minInterviews:
            raise ValueError("maxInterviews must be >= minInterviews")
        return self


class SkillUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    minInterviews: Optional[int] = Field(default=None, ge=1)
    maxInterviews: Optional[int] = Field(default=None, ge=1)
    systemPrompt: Optional[str] = Field(default=None, max_length=20000)


@router.get("")
async def list_skills(
    _: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    docs = await Skill.find({}).sort([("title", 1)]).to_list()
    return {"skills": [doc_to_dict(d) for d in docs]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: SkillIn,
    user: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    existing = await Skill.find_one(Skill.title == body.title)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A skill with this title already exists")

    skill = Skill(
        title=body.title,
        minInterviews=body.minInterviews,
        maxInterviews=body.maxInterviews,
        systemPrompt=body.systemPrompt,
        createdBy=user.id,
    )
    await skill.insert()
    return doc_to_dict(skill)


@router.patch("/{skill_id}")
async def update_skill(
    skill_id: str,
    body: SkillUpdate,
    _: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    oid = to_object_id(skill_id)
    skill = await Skill.get(oid)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    if body.title is not None:
        if body.title != skill.title:
            clash = await Skill.find_one(Skill.title == body.title)
            if clash and clash.id != skill.id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A skill with this title already exists")
        skill.title = body.title
    if body.minInterviews is not None:
        skill.minInterviews = body.minInterviews
    if body.maxInterviews is not None:
        skill.maxInterviews = body.maxInterviews
    if body.systemPrompt is not None:
        skill.systemPrompt = body.systemPrompt

    if skill.maxInterviews < skill.minInterviews:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="maxInterviews must be >= minInterviews")

    await skill.save()
    return doc_to_dict(skill)


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: str,
    _: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    oid = to_object_id(skill_id)
    skill = await Skill.get(oid)
    if skill is not None:
        await skill.delete()
    return {"ok": True}
