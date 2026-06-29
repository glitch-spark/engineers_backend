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


class AccountCreate(BaseModel):
    name: str
    resumes: Optional[List[ResumeIn]] = None
    resumePromptBody: Optional[str] = None
    styleTemplate: Optional[str] = None
    styleTemplateName: Optional[str] = None
    pageFormat: Optional[str] = None
    previewHtml: Optional[str] = None
    screeningPrompt: Optional[str] = None
    coverLetterPrompt: Optional[str] = None
    llmProvider: Optional[str] = None


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    resumes: Optional[List[ResumeIn]] = None
    resumePromptBody: Optional[str] = None
    styleTemplate: Optional[str] = None
    styleTemplateName: Optional[str] = None
    pageFormat: Optional[str] = None
    previewHtml: Optional[str] = None
    screeningPrompt: Optional[str] = None
    coverLetterPrompt: Optional[str] = None
    llmProvider: Optional[str] = None


@router.get("/lookup")
async def lookup_accounts(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Minimal projection of every account for filter dropdowns. Auth required,
    no scoping. `hasTemplate` lets the Resume Generator label / gate accounts
    without a second round-trip — a profile is generation-ready iff it has an
    uploaded template."""
    rows = await Account.find({}).to_list()
    return {
        "accounts": [
            {
                "_id": str(a.id),
                "name": a.name,
                "hasTemplate": bool((a.styleTemplate or "").strip()),
                "hasPrompt": bool((a.resumePromptBody or "").strip()),
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
        query["name"] = ci_regex(search)

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
        createdBy=user.id,
        resumes=resumes,
        resumePromptBody=body.resumePromptBody,
        styleTemplate=body.styleTemplate,
        styleTemplateName=body.styleTemplateName,
        previewHtml=body.previewHtml,
        screeningPrompt=body.screeningPrompt,
        coverLetterPrompt=body.coverLetterPrompt,
    )
    if body.pageFormat is not None:
        kwargs["pageFormat"] = body.pageFormat
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
    if body.resumes is not None:
        acc.resumes = _validate_and_normalize_resumes(body.resumes)
    if body.resumePromptBody is not None:
        acc.resumePromptBody = body.resumePromptBody
    if body.styleTemplate is not None:
        from app.html_sanitize import sanitize_template_html
        acc.styleTemplate = sanitize_template_html(body.styleTemplate)
    if body.styleTemplateName is not None:
        acc.styleTemplateName = body.styleTemplateName
    if body.pageFormat is not None:
        acc.pageFormat = body.pageFormat  # type: ignore[assignment]
    if body.previewHtml is not None:
        acc.previewHtml = body.previewHtml
    if body.screeningPrompt is not None:
        acc.screeningPrompt = body.screeningPrompt
    if body.coverLetterPrompt is not None:
        acc.coverLetterPrompt = body.coverLetterPrompt
    if body.llmProvider is not None:
        if body.llmProvider == "":
            acc.llmProvider = None
        elif body.llmProvider in ("openai", "nvidia_free"):
            acc.llmProvider = body.llmProvider  # type: ignore[assignment]
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid llmProvider")


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
