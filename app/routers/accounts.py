from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.models import Account, UserExtra
from app.routers._util import ci_regex, doc_to_dict, populate_owner, to_object_id

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    name: str
    email: str
    phone: Optional[str] = ""
    address: Optional[str] = ""


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None


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


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    acc = Account(
        name=body.name,
        email=body.email,
        phone=body.phone or "",
        address=body.address or "",
        createdBy=user.id,
    )
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
