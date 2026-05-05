import re
from typing import Annotated, Any, List, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.models import Account, UserExtra
from app.models.account import Resume
from app.routers._util import ci_regex, doc_to_dict, populate_owner, to_object_id

router = APIRouter(prefix="/accounts", tags=["accounts"])

# Schema bounds — see Step 1 ADR.
MAX_RESUMES_PER_ACCOUNT = 20
MAX_RESUME_MARKDOWN_BYTES = 200_000
MAX_FILENAME_LEN = 255

# Drop control chars (incl. NUL) and path separators. Keeps the filename safe to
# echo back in headers/UI without further escaping.
_FILENAME_BAD = re.compile(r"[\x00-\x1f\x7f/\\]")


class ResumeIn(BaseModel):
    """Resume input shape. `id` is optional: present for existing resumes (preserves identity),
    absent for newly-staged uploads (server assigns a fresh ObjectId)."""
    id: Optional[str] = None
    filename: str
    markdown: str


def _sanitize_filename(raw: str) -> str:
    return _FILENAME_BAD.sub("_", raw or "").strip()


def _validate_and_normalize_resumes(items: List[ResumeIn]) -> List[Resume]:
    if len(items) > MAX_RESUMES_PER_ACCOUNT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"too many resumes (max {MAX_RESUMES_PER_ACCOUNT})",
        )
    out: List[Resume] = []
    for item in items:
        fn = _sanitize_filename(item.filename)
        if not fn or len(fn) > MAX_FILENAME_LEN:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid filename")
        if len(item.markdown.encode("utf-8")) > MAX_RESUME_MARKDOWN_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"resume markdown exceeds {MAX_RESUME_MARKDOWN_BYTES} bytes",
            )
        rid = PydanticObjectId(item.id) if item.id else PydanticObjectId()
        out.append(Resume(id=rid, filename=fn, markdown=item.markdown))
    return out


def _strip_markdown_from_account(payload: dict) -> dict:
    """Replace each resume's `markdown` with `sizeBytes` for list responses.
    Keeps id/filename/uploadedAt so the SPA can show counts and filenames; the
    full markdown only ships from the per-id GET endpoint when the user clicks Edit."""
    resumes = payload.get("resumes")
    if isinstance(resumes, list):
        slim = []
        for r in resumes:
            if isinstance(r, dict):
                slim.append({
                    "id": r.get("id"),
                    "filename": r.get("filename"),
                    "uploadedAt": r.get("uploadedAt"),
                    "sizeBytes": len((r.get("markdown") or "").encode("utf-8")),
                })
        payload["resumes"] = slim
    return payload


class CustomLinkIn(BaseModel):
    label: str
    url: str


class AccountCreate(BaseModel):
    name: str
    email: str
    phone: Optional[str] = ""
    address: Optional[str] = ""
    resumes: Optional[List[ResumeIn]] = None
    label: Optional[str] = None
    education: Optional[str] = None
    experience: Optional[str] = None
    styles: Optional[str] = None
    github: Optional[str] = None
    linkedin: Optional[str] = None
    website: Optional[str] = None
    twitter: Optional[str] = None
    customLinks: Optional[List[CustomLinkIn]] = None
    contactLabels: Optional[dict] = None
    styleMode: Optional[str] = None
    styleMarkdown: Optional[str] = None
    styleConfig: Optional[dict] = None
    pageFormat: Optional[str] = None
    previewHtml: Optional[str] = None
    screeningPromptMode: Optional[str] = None
    screeningPrompt: Optional[str] = None


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    resumes: Optional[List[ResumeIn]] = None
    label: Optional[str] = None
    education: Optional[str] = None
    experience: Optional[str] = None
    styles: Optional[str] = None
    github: Optional[str] = None
    linkedin: Optional[str] = None
    website: Optional[str] = None
    twitter: Optional[str] = None
    customLinks: Optional[List[CustomLinkIn]] = None
    contactLabels: Optional[dict] = None
    styleMode: Optional[str] = None
    styleMarkdown: Optional[str] = None
    styleConfig: Optional[dict] = None
    pageFormat: Optional[str] = None
    previewHtml: Optional[str] = None
    screeningPromptMode: Optional[str] = None
    screeningPrompt: Optional[str] = None


@router.get("/lookup")
async def lookup_accounts(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Minimal projection of every account, for filter dropdowns. Auth required, no scoping.
    `label` and `hasExperience` are surfaced so the Resume Generator dropdown can label
    and gate accounts without a second round-trip."""
    rows = await Account.find({}).to_list()
    return {
        "accounts": [
            {
                "_id": str(a.id),
                "name": a.name,
                "email": a.email,
                "label": a.label,
                "hasExperience": bool((a.experience or "").strip()),
                "createdBy": str(a.createdBy),
            }
            for a in rows
        ]
    }


@router.get("")
async def list_accounts(
    user: Annotated[UserExtra, Depends(get_current_user)],
    page: int = Query(1),
    limit: int = Query(10),
    search: str = Query(""),
    userId: str = Query(""),
) -> dict:
    is_admin = user.role == "admin"
    query: dict[str, Any] = {}
    if is_admin:
        if userId:
            query["createdBy"] = to_object_id(userId)
    else:
        query["createdBy"] = user.id

    if search.strip():
        query["$or"] = [
            {"name": ci_regex(search)},
            {"email": ci_regex(search)},
            {"phone": ci_regex(search)},
            {"address": ci_regex(search)},
        ]

    skip = (page - 1) * limit
    total = await Account.find(query).count()
    rows = await Account.find(query).skip(skip).limit(limit).to_list()
    accounts_out = await populate_owner(rows, field="createdBy")
    # Strip the markdown blob from list responses to prevent multi-MB payloads.
    # Full resume content ships only from GET /accounts/{id} (used by Edit modal).
    accounts_out = [_strip_markdown_from_account(a) for a in accounts_out]

    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "accounts": accounts_out,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "totalPages": total_pages,
            "hasNext": page < total_pages,
            "hasPrev": page > 1,
        },
    }


@router.get("/{account_id}")
async def get_account(
    account_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Full account incl. resume markdown. Owner-or-admin scoped, mirroring update/delete."""
    oid = to_object_id(account_id)
    acc = await Account.get(oid)
    is_admin = user.role == "admin"
    if acc is None or (not is_admin and str(acc.createdBy) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return doc_to_dict(acc)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    resumes = _validate_and_normalize_resumes(body.resumes or [])
    kwargs: dict = dict(
        name=body.name,
        email=body.email,
        phone=body.phone or "",
        address=body.address or "",
        createdBy=user.id,
        resumes=resumes,
        label=body.label,
        education=body.education,
        experience=body.experience,
        styles=body.styles,
        github=body.github,
        linkedin=body.linkedin,
        website=body.website,
        twitter=body.twitter,
        customLinks=[link.model_dump() for link in (body.customLinks or [])],
        contactLabels=body.contactLabels or {},
        styleMarkdown=body.styleMarkdown,
        styleConfig=body.styleConfig,
        previewHtml=body.previewHtml,
        screeningPrompt=body.screeningPrompt,
    )
    if body.styleMode is not None:
        kwargs["styleMode"] = body.styleMode
    if body.pageFormat is not None:
        kwargs["pageFormat"] = body.pageFormat
    if body.screeningPromptMode is not None:
        kwargs["screeningPromptMode"] = body.screeningPromptMode
    acc = Account(**kwargs)
    await acc.insert()
    return doc_to_dict(acc)


@router.put("/{account_id}")
async def update_account(
    account_id: str,
    body: AccountUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(account_id)
    acc = await Account.get(oid)
    if acc is None or str(acc.createdBy) != str(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    if body.name is not None:
        acc.name = body.name
    if body.email is not None:
        acc.email = body.email
    if body.phone is not None:
        acc.phone = body.phone
    if body.address is not None:
        acc.address = body.address
    if body.resumes is not None:
        acc.resumes = _validate_and_normalize_resumes(body.resumes)
    if body.label is not None:
        acc.label = body.label
    if body.education is not None:
        acc.education = body.education
    if body.experience is not None:
        acc.experience = body.experience
    if body.styles is not None:
        acc.styles = body.styles
    if body.github is not None:
        acc.github = body.github
    if body.linkedin is not None:
        acc.linkedin = body.linkedin
    if body.website is not None:
        acc.website = body.website
    if body.twitter is not None:
        acc.twitter = body.twitter
    if body.customLinks is not None:
        from app.models.account import CustomLink
        acc.customLinks = [CustomLink(label=link.label, url=link.url) for link in body.customLinks]
    if body.contactLabels is not None:
        acc.contactLabels = body.contactLabels
    if body.styleMode is not None:
        acc.styleMode = body.styleMode  # type: ignore[assignment]
    if body.styleMarkdown is not None:
        acc.styleMarkdown = body.styleMarkdown
    if body.styleConfig is not None:
        acc.styleConfig = body.styleConfig
    if body.pageFormat is not None:
        acc.pageFormat = body.pageFormat  # type: ignore[assignment]
    if body.previewHtml is not None:
        acc.previewHtml = body.previewHtml
    if body.screeningPromptMode is not None:
        acc.screeningPromptMode = body.screeningPromptMode  # type: ignore[assignment]
    if body.screeningPrompt is not None:
        acc.screeningPrompt = body.screeningPrompt

    await acc.save()
    return doc_to_dict(acc)


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(account_id)
    acc = await Account.get(oid)
    if acc is None or str(acc.createdBy) != str(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    await acc.delete()
    return {"ok": True}
