from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.deps import get_current_user
from app.models import Account, ResumeApplication, UserExtra
from app.resume_service import generate_resume
from app.routers._util import doc_to_dict, to_object_id

router = APIRouter(prefix="/resume", tags=["resume"])

MAX_JD_BYTES = 50_000


class GenerateBody(BaseModel):
    accountId: str
    company: str = Field(min_length=1)
    jobDescription: str = Field(min_length=1)


async def _load_owned_account(account_id: str, user: UserExtra) -> Account:
    account = await Account.get(to_object_id(account_id))
    is_admin = user.role == "admin"
    if account is None or (not is_admin and str(account.createdBy) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return account


@router.post("/generate")
async def generate(
    body: GenerateBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> Response:
    if len(body.jobDescription.encode("utf-8")) > MAX_JD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"jobDescription exceeds {MAX_JD_BYTES} bytes",
        )

    account = await _load_owned_account(body.accountId, user)

    # Resume generation needs at least an experience blob to produce useful output.
    if not (account.experience or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is missing the 'experience' field — fill it on the Accounts page first.",
        )

    company = body.company.strip()
    company_lower = company.lower()

    existing = await ResumeApplication.find_one(
        {
            "userId": user.id,
            "accountId": account.id,
            "companyNameLower": company_lower,
        }
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You've already applied for this company with this account.",
        )

    result = await generate_resume(account, body.jobDescription, company)

    application = ResumeApplication(
        userId=user.id,
        accountId=account.id,
        companyName=company,
        companyNameLower=company_lower,
        jobDescription=body.jobDescription,
        s3Key=result.s3_key,
        s3Url=result.s3_url,
    )
    try:
        await application.insert()
    except DuplicateKeyError:
        pass

    headers = {
        "Content-Disposition": f'attachment; filename="resume_{company.replace(" ", "_")}.pdf"',
    }
    if result.s3_url:
        headers["X-Resume-URL"] = result.s3_url

    return Response(content=result.pdf_bytes, media_type="application/pdf", headers=headers)


@router.get("/history")
async def history(
    user: Annotated[UserExtra, Depends(get_current_user)],
    accountId: Optional[str] = None,
) -> dict:
    query: dict = {} if user.role == "admin" else {"userId": user.id}
    if accountId:
        query["accountId"] = to_object_id(accountId)
    rows = await ResumeApplication.find(query).sort("-createdAt").to_list()
    return {"applications": [doc_to_dict(r) for r in rows]}
