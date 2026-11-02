import os
import re
from types import SimpleNamespace
from datetime import datetime, timezone
from typing import Annotated, Any, List, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from app import resume_files
from app.deps import get_current_user
from app.models import Account, UserExtra
from app.models.account import PromptRole, Resume, ResumePrompt
from app.resume_prompts import (
    PromptError, apply_prompts, max_body, prompts_view, sync_default_body, sync_default_template,
)
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


def _validate_and_normalize_resumes(items: List[ResumeIn], existing: Optional[List[Resume]] = None) -> List[Resume]:
    """`existing` is the profile's current list: resumes that keep their id keep their upload date."""
    previous = {str(r.id): r for r in existing or []}
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
        prev = previous.get(str(rid))
        kept = (
            {"uploadedAt": prev.uploadedAt, "s3Key": prev.s3Key, "fileSize": prev.fileSize, "fileType": prev.fileType}
            if prev else {}
        )
        out.append(Resume(id=rid, filename=fn, markdown=item.markdown, **kept))
    return out


def _slim_units(payload: dict) -> None:
    """Units in list responses: no prompt text or template HTML (up to 10 x 30 KB + 2 MB each per profile)."""
    units = payload.get("resumePrompts")
    if isinstance(units, list):
        payload["resumePrompts"] = [
            {k: v for k, v in u.items() if k not in ("body", "templateHtml")} if isinstance(u, dict) else u for u in units
        ]


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
                    "hasFile": bool(r.get("s3Key")),
                })
        payload["resumes"] = slim
    _slim_units(payload)
    return payload


class AccountCreate(BaseModel):
    name: str
    country: Optional[str] = None
    region: Optional[str] = None
    resumes: Optional[List[ResumeIn]] = None
    resumePromptBody: Optional[str] = None
    styleTemplate: Optional[str] = None
    styleTemplateName: Optional[str] = None
    pageFormat: Optional[str] = None
    previewHtml: Optional[str] = None
    screeningPrompt: Optional[str] = None
    coverLetterPrompt: Optional[str] = None


class ResumePromptIn(BaseModel):
    """A resume unit as the app sends it."""
    id: Optional[str] = None  # missing or "new-…" = a new unit; the server gives it an id
    name: str
    roles: List[PromptRole] = []
    role: Optional[PromptRole] = None  # older screens: one role
    body: str = ""
    templateHtml: Optional[str] = None  # None = not sent (keep the unit's template)
    templateName: Optional[str] = None
    pageFormat: Optional[str] = None


def _sanitized(html: Optional[str]) -> Optional[str]:
    from app.html_sanitize import sanitize_template_html
    return sanitize_template_html(html) if html else html


def _account_out(acc: Account) -> dict:
    """The account as the app reads it, with its resume prompts (an older profile's single prompt as "Default")."""
    prompts, default_id = prompts_view(acc)
    return {
        **doc_to_dict(acc),
        "resumePrompts": [p.model_dump(mode="json") for p in prompts],
        "defaultPromptId": default_id,
        "resumePromptBody": next((p.body for p in prompts if p.id == default_id), "") or None,
    }


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    resumes: Optional[List[ResumeIn]] = None
    resumePromptBody: Optional[str] = None
    # The whole list of resume prompts and the default; when sent, resumePromptBody follows the default prompt.
    resumePrompts: Optional[List[ResumePromptIn]] = None
    defaultPromptId: Optional[str] = None
    styleTemplate: Optional[str] = None
    styleTemplateName: Optional[str] = None
    pageFormat: Optional[str] = None
    previewHtml: Optional[str] = None
    screeningPrompt: Optional[str] = None
    coverLetterPrompt: Optional[str] = None
    showInGenerate: Optional[bool] = None
    archived: Optional[bool] = None


def archive_status_query(status_: str) -> dict:
    """Mongo filter for the Profiles status filter (default: active).

    Profiles saved before archiving existed have no `archived` field, so
    "active" matches anything that isn't explicitly archived.
    """
    key = (status_ or "active").strip().lower()
    if key == "active":
        return {"archived": {"$ne": True}}
    if key == "archived":
        return {"archived": True}
    if key == "all":
        return {}
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be active, archived or all")


ALLOWED_REGIONS = frozenset({"Latam", "EU", "Canada", "US", "Asia"})


def _normalize_country(raw: Optional[str]) -> Optional[str]:
    """Store ISO alpha-2 uppercase, or None when cleared/empty."""
    if raw is None:
        return None
    code = (raw or "").strip().upper()
    if not code:
        return None
    if len(code) != 2 or not code.isalpha():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="country must be a 2-letter ISO code",
        )
    return code


def _normalize_region(raw: Optional[str]) -> Optional[str]:
    """Store one of ALLOWED_REGIONS, or None when cleared/empty."""
    if raw is None:
        return None
    value = (raw or "").strip()
    if not value:
        return None
    if value not in ALLOWED_REGIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"region must be one of: {', '.join(sorted(ALLOWED_REGIONS))}",
        )
    return value


def _non_blank(field: str) -> dict:
    """Mongo expression: the string field is present and not just whitespace (like `bool((x or "").strip())`)."""
    return {"$gt": [{"$strLenCP": {"$trim": {"input": {"$ifNull": [f"${field}", ""]}}}}, 0]}


# Profiles carry large HTML templates and resume text, so the lookup computes its flags inside the
# database and returns only small fields. Downloading whole documents for the dropdown took ~18 s.
_LOOKUP_PIPELINE = [
    {
        "$project": {
            "name": 1,
            "country": 1,
            "region": 1,
            "createdBy": 1,
            "showInGenerate": 1,
            "archived": 1,
            "hasTemplate": _non_blank("styleTemplate"),
            "hasPrompt": _non_blank("resumePromptBody"),
            # Resume units, small fields only. tpl: null = saved before units had templates, else has HTML.
            "units": {
                "$map": {
                    "input": {"$ifNull": ["$resumePrompts", []]},
                    "as": "u",
                    "in": {
                        "id": "$$u.id",
                        "name": "$$u.name",
                        "roles": "$$u.roles",
                        "role": "$$u.role",
                        "templateName": "$$u.templateName",
                        "tpl": {"$cond": [
                            {"$eq": [{"$ifNull": ["$$u.templateHtml", None]}, None]},
                            None,
                            {"$gt": [{"$strLenCP": "$$u.templateHtml"}, 0]},
                        ]},
                    },
                }
            },
            "defaultPromptId": 1,
            "styleTemplateName": 1,
            # An older profile's single unit is named from its prompt's heading.
            "promptHead": {"$substrCP": [{"$ifNull": ["$resumePromptBody", ""]}, 0, 400]},
        }
    }
]


def _lookup_units(a: dict) -> dict:
    """The profile's units as Job Applies lists them, read with the same rules (prompts_view) from the slim lookup
    fields: placeholders stand in for template HTML and prompt texts, which never leave the database."""
    def stand_in(tpl):
        return None if tpl is None else ("x" if tpl else "")

    stored = [
        ResumePrompt.model_construct(
            id=u.get("id"), name=u.get("name") or "", roles=list(u.get("roles") or []), role=u.get("role"), body="",
            templateHtml=stand_in(u.get("tpl")), templateName=u.get("templateName"), pageFormat="A4",
        )
        for u in a.get("units") or [] if u.get("id")
    ]
    view = SimpleNamespace(
        resumePrompts=stored, defaultPromptId=a.get("defaultPromptId"),
        styleTemplate="x" if a.get("hasTemplate") else "", styleTemplateName=a.get("styleTemplateName"),
        pageFormat="A4", resumePromptBody=a.get("promptHead") or "",
    )
    units, default_id = prompts_view(view)
    return {
        "units": [{"id": u.id, "name": u.name, "roles": list(u.roles), "hasTemplate": bool(u.templateHtml),
                   "templateName": u.templateName or None} for u in units],
        "defaultPromptId": default_id,
    }


@router.get("/lookup")
async def lookup_accounts(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Minimal projection of every account for filter dropdowns. Auth required,
    no scoping. `hasTemplate` lets the Resume Generator label / gate accounts
    without a second round-trip — a profile is generation-ready iff it has an
    uploaded template."""
    rows = await Account.get_motor_collection().aggregate(_LOOKUP_PIPELINE).to_list(None)
    return {
        "accounts": [
            {
                "_id": str(a["_id"]),
                "name": a.get("name"),
                "country": a.get("country"),
                "region": a.get("region"),
                "hasTemplate": bool(a.get("hasTemplate")),
                "hasPrompt": bool(a.get("hasPrompt")),
                "createdBy": str(a.get("createdBy")),
                # Profiles saved before these fields existed are active and shown.
                "showInGenerate": a.get("showInGenerate", True),
                "archived": a.get("archived", False),
                **_lookup_units(a),
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
    status_: str = Query("active", alias="status"),
) -> dict:
    is_admin = user.role == "admin"
    query: dict[str, Any] = archive_status_query(status_)
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
    return _account_out(acc)


def _cut_prompt(text: Optional[str]) -> Optional[str]:
    """A screening / cover-letter prompt cut to RESUME_PROMPT_MAX_CHARS, like resume prompts."""
    return text[:max_body()] if text else text


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    resumes = _validate_and_normalize_resumes(body.resumes or [])
    kwargs: dict = dict(
        name=body.name,
        country=_normalize_country(body.country),
        region=_normalize_region(body.region),
        createdBy=user.id,
        resumes=resumes,
        resumePromptBody=body.resumePromptBody,
        styleTemplate=_sanitized(body.styleTemplate),
        styleTemplateName=body.styleTemplateName,
        previewHtml=body.previewHtml,
        screeningPrompt=_cut_prompt(body.screeningPrompt),
        coverLetterPrompt=_cut_prompt(body.coverLetterPrompt),
    )
    if body.pageFormat is not None:
        kwargs["pageFormat"] = body.pageFormat
    acc = Account(**kwargs)
    await acc.insert()
    return doc_to_dict(acc)


async def _editable_account(account_id: str, user: UserExtra) -> Account:
    """The profile if `user` may change it: its creator, or any admin (archive, edit, delete). 404 otherwise."""
    acc = await Account.get(to_object_id(account_id))
    if acc is None or (user.role != "admin" and str(acc.createdBy) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return acc


@router.put("/{account_id}")
async def update_account(
    account_id: str,
    body: AccountUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    acc = await _editable_account(account_id, user)

    if body.name is not None:
        acc.name = body.name
    if body.country is not None:
        # Allow clearing with "" — normalize to None.
        acc.country = _normalize_country(body.country)
    if body.region is not None:
        acc.region = _normalize_region(body.region)
    removed_files: list[str] = []
    if body.resumes is not None:
        before = list(acc.resumes)
        acc.resumes = _validate_and_normalize_resumes(body.resumes, before)
        kept_ids = {str(r.id) for r in acc.resumes}
        removed_files = [r.s3Key for r in before if r.s3Key and str(r.id) not in kept_ids]
    if body.resumePrompts is not None:
        try:
            apply_prompts(acc, [p.model_dump() for p in body.resumePrompts], body.defaultPromptId)
        except PromptError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    else:  # older screens write one field: it changes just the default unit
        if body.resumePromptBody is not None:
            sync_default_body(acc, body.resumePromptBody)
        if body.styleTemplate is not None or body.styleTemplateName is not None:
            from app.html_sanitize import sanitize_template_html
            html = sanitize_template_html(body.styleTemplate) if body.styleTemplate is not None else None
            sync_default_template(acc, html, body.styleTemplateName)
    if body.pageFormat is not None:
        acc.pageFormat = body.pageFormat  # type: ignore[assignment]
    if body.previewHtml is not None:
        acc.previewHtml = body.previewHtml
    if body.screeningPrompt is not None:
        acc.screeningPrompt = _cut_prompt(body.screeningPrompt)
    if body.coverLetterPrompt is not None:
        acc.coverLetterPrompt = _cut_prompt(body.coverLetterPrompt)
    if body.showInGenerate is not None:
        acc.showInGenerate = body.showInGenerate
    if body.archived is not None and body.archived != acc.archived:
        acc.archived = body.archived
        acc.archivedAt = datetime.now(timezone.utc) if body.archived else None

    await acc.save()
    await resume_files.delete_resume_files(removed_files)
    return _account_out(acc)


@router.post("/{account_id}/resumes")
async def upload_resume(
    account_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    file: UploadFile = File(...),
    markdown: str = Form(...),
) -> dict:
    """Add (or replace, by filename) one resume: the browser-extracted text goes in MongoDB, the original
    file in S3 when configured. Returns the full account like PUT."""
    acc = await _editable_account(account_id, user)
    fn = _sanitize_filename(file.filename or "")
    ext = os.path.splitext(fn)[1].lower()
    if not fn or len(fn) > MAX_FILENAME_LEN or ext not in resume_files.ALLOWED_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload a .pdf, .txt or .md resume.")
    if not markdown.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No text could be read from this resume.")
    if len(markdown.encode("utf-8")) > MAX_RESUME_MARKDOWN_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"resume markdown exceeds {MAX_RESUME_MARKDOWN_BYTES} bytes"
        )
    data = await file.read(resume_files.MAX_FILE_BYTES + 1)
    if len(data) > resume_files.MAX_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File is larger than 10 MB.")

    current = next((r for r in acc.resumes if r.filename.lower() == fn.lower()), None)
    if current is None and len(acc.resumes) >= MAX_RESUMES_PER_ACCOUNT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"too many resumes (max {MAX_RESUMES_PER_ACCOUNT})"
        )
    rid = current.id if current else PydanticObjectId()
    key = resume_files.resume_key(str(acc.id), str(rid), ext)
    content_type = resume_files.ALLOWED_TYPES[ext]
    stored = await resume_files.put_resume_file(key, data, content_type)
    resume = Resume(
        id=rid, filename=fn, markdown=markdown,
        s3Key=key if stored else None, fileSize=len(data), fileType=content_type,
    )
    stale = current.s3Key if current and current.s3Key and current.s3Key != resume.s3Key else None
    acc.resumes = [resume if r.id == rid else r for r in acc.resumes] if current else [*acc.resumes, resume]
    await acc.save()
    if stale:
        await resume_files.delete_resume_files([stale])
    return doc_to_dict(acc)


@router.get("/{account_id}/resumes/{resume_id}/file")
async def resume_file_url(
    account_id: str,
    resume_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Short-lived download link for a resume's original file. Owner, admin, or an interview manager (they open
    everyone's interviews, so they can save the resume an interview came from)."""
    acc = await Account.get(to_object_id(account_id))
    if acc is None or (user.role not in ("admin", "interview_manager") and str(acc.createdBy) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    resume = next((r for r in acc.resumes if str(r.id) == resume_id), None)
    if resume is None or not resume.s3Key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The original file isn't stored for this resume.")
    return {"url": resume_files.presigned_url(resume.s3Key, resume.filename)}


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    acc = await _editable_account(account_id, user)
    files = [r.s3Key for r in acc.resumes if r.s3Key]
    await acc.delete()
    await resume_files.delete_resume_files(files)
    return {"ok": True}
