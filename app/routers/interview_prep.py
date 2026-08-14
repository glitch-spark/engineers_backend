from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app.models import InterviewPrompt, InterviewTemplateAnswer, UserExtra
from app.routers._util import populate_owner, to_object_id

router = APIRouter(prefix="/interview-prep", tags=["interview-prep"])


class PrepItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=50000)


class PrepItemUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    body: Optional[str] = Field(default=None, max_length=50000)


def _assert_owner(doc, user: UserExtra) -> None:
    if str(doc.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


@router.get("/prompts")
async def list_prompts(
    _: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
) -> dict:
    query: dict = {}
    if userId:
        query["userId"] = to_object_id(userId)
    rows = await InterviewPrompt.find(query).sort([("updatedAt", -1)]).to_list()
    out = await populate_owner(rows, field="userId", only=("name", "email"))
    return {"prompts": out}


@router.post("/prompts", status_code=status.HTTP_201_CREATED)
async def create_prompt(
    body: PrepItemIn,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    doc = InterviewPrompt(userId=user.id, title=body.title.strip(), body=body.body or "")
    await doc.insert()
    populated = await populate_owner([doc], field="userId", only=("name", "email"))
    return populated[0]


@router.put("/prompts/{item_id}")
async def update_prompt(
    item_id: str,
    body: PrepItemUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(item_id)
    doc = await InterviewPrompt.get(oid)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    _assert_owner(doc, user)
    if body.title is not None:
        doc.title = body.title.strip()
    if body.body is not None:
        doc.body = body.body
    await doc.save()
    populated = await populate_owner([doc], field="userId", only=("name", "email"))
    return populated[0]


@router.delete("/prompts/{item_id}")
async def delete_prompt(
    item_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(item_id)
    doc = await InterviewPrompt.get(oid)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    _assert_owner(doc, user)
    await doc.delete()
    return {"ok": True}


@router.get("/template-answers")
async def list_template_answers(
    _: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
) -> dict:
    query: dict = {}
    if userId:
        query["userId"] = to_object_id(userId)
    rows = await InterviewTemplateAnswer.find(query).sort([("updatedAt", -1)]).to_list()
    out = await populate_owner(rows, field="userId", only=("name", "email"))
    return {"items": out}


@router.post("/template-answers", status_code=status.HTTP_201_CREATED)
async def create_template_answer(
    body: PrepItemIn,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    doc = InterviewTemplateAnswer(userId=user.id, title=body.title.strip(), body=body.body or "")
    await doc.insert()
    populated = await populate_owner([doc], field="userId", only=("name", "email"))
    return populated[0]


@router.put("/template-answers/{item_id}")
async def update_template_answer(
    item_id: str,
    body: PrepItemUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(item_id)
    doc = await InterviewTemplateAnswer.get(oid)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    _assert_owner(doc, user)
    if body.title is not None:
        doc.title = body.title.strip()
    if body.body is not None:
        doc.body = body.body
    await doc.save()
    populated = await populate_owner([doc], field="userId", only=("name", "email"))
    return populated[0]


@router.delete("/template-answers/{item_id}")
async def delete_template_answer(
    item_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(item_id)
    doc = await InterviewTemplateAnswer.get(oid)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    _assert_owner(doc, user)
    await doc.delete()
    return {"ok": True}
