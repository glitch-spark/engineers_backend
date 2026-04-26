import re
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.deps import get_current_user
from app.models import CardLink, UserExtra
from app.routers._util import ci_regex, doc_to_dict, parse_date, populate_owner, to_object_id

router = APIRouter(prefix="/cardlinks", tags=["cardlinks"])


class CardLinkCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    email: str
    cardNumber: str
    site: str
    from_: str = Field(alias="from")
    to: str


class CardLinkUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    site: Optional[str] = None
    cardNumber: Optional[str] = None
    status: Optional[str] = None
    email: Optional[str] = None


@router.get("")
async def list_cardlinks(
    user: Annotated[UserExtra, Depends(get_current_user)],
    page: int = Query(1),
    limit: int = Query(10),
    from_: str = Query("", alias="from"),
    to: str = Query(""),
    userId: str = Query(""),
    search: str = Query(""),
    fromSearch: str = Query(""),
    toSearch: str = Query(""),
    userSearch: str = Query(""),
) -> dict:
    is_admin = user.role == "admin"
    query: dict[str, Any] = {} if is_admin else {"userId": user.id}

    date_filter: dict = {}
    f_dt = parse_date(from_)
    t_dt = parse_date(to, end_of_day=True)
    if f_dt:
        date_filter["$gte"] = f_dt
    if t_dt:
        date_filter["$lte"] = t_dt
    if date_filter:
        query["from"] = date_filter

    if is_admin and userId:
        query["userId"] = to_object_id(userId)

    if search:
        query["$or"] = [
            {"cardNumber": ci_regex(search)},
            {"site": ci_regex(search)},
            {"userId.name": ci_regex(search)},
            {"userId.email": ci_regex(search)},
        ]

    extra: list[dict] = []
    if fromSearch:
        extra.append({"$expr": {"$regexMatch": {
            "input": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "regex": re.escape(fromSearch),
            "options": "i",
        }}})
    if toSearch:
        extra.append({"$expr": {"$regexMatch": {
            "input": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "regex": re.escape(toSearch),
            "options": "i",
        }}})
    if userSearch:
        extra.append({"$or": [
            {"userId.name": ci_regex(userSearch)},
            {"userId.email": ci_regex(userSearch)},
        ]})
    if extra:
        query["$and"] = extra

    skip = (page - 1) * limit
    total = await CardLink.find(query).count()
    rows = await CardLink.find(query).sort([("date", -1)]).skip(skip).limit(limit).to_list()
    cls_out = await populate_owner(rows, field="userId")

    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "cardlinks": cls_out,
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
async def create_cardlink(
    body: CardLinkCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if not body.email or not body.cardNumber or not body.from_ or not body.to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Fill in all required fields")

    f_dt = parse_date(body.from_)
    t_dt = parse_date(body.to)
    if f_dt is None or t_dt is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid dates")

    cl = CardLink(
        userId=user.id,
        from_=f_dt,
        to=t_dt,
        site=body.site,
        cardNumber=body.cardNumber,
        email=body.email,
        status="billing",
    )
    await cl.insert()
    return doc_to_dict(cl)


@router.put("/{cl_id}")
async def update_cardlink(
    cl_id: str,
    body: CardLinkUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(cl_id)
    cl = await CardLink.get(oid)
    if cl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    is_admin = user.role == "admin"
    is_owner = str(cl.userId) == str(user.id)
    if not is_admin and not is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    # Mirrors source guard `cl.status !== 'cenceled'` (typo); effectively no-op since real values are billing/canceled.
    if cl.status != "cenceled" or is_admin:
        if body.from_ is not None:
            d = parse_date(body.from_)
            if d is not None:
                cl.from_ = d
        if body.to is not None:
            d = parse_date(body.to)
            if d is not None:
                cl.to = d
        if body.site is not None:
            cl.site = body.site
        if body.cardNumber is not None:
            cl.cardNumber = body.cardNumber
        if body.status is not None:
            cl.status = body.status
        if body.email is not None:
            cl.email = body.email

    await cl.save()
    return doc_to_dict(cl)


@router.delete("/{cl_id}")
async def delete_cardlink(
    cl_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(cl_id)
    cl = await CardLink.get(oid)
    if cl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    is_admin = user.role == "admin"
    is_owner = str(cl.userId) == str(user.id)
    if not is_admin and not is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    await cl.delete()
    return {"ok": True}
