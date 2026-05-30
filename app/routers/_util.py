import re
from datetime import datetime
from typing import Iterable, Optional

from beanie import Document, PydanticObjectId
from fastapi import HTTPException, status

from app.models import UserExtra


def to_object_id(value: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(value)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid id")


def ci_regex(value: str) -> dict:
    return {"$regex": re.escape(value), "$options": "i"}


def doc_to_dict(doc: Document) -> dict:
    payload = doc.model_dump(by_alias=True, mode="json")
    payload.pop("hashedPassword", None)
    if "id" in payload and "_id" not in payload:
        payload["_id"] = payload.pop("id")
    return payload


def parse_date(s: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    if not s:
        return None
    try:
        if end_of_day and len(s) == 10:
            return datetime.fromisoformat(f"{s}T23:59:59.999")
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


async def populate_owner(
    docs: Iterable[Document],
    *,
    field: str,
    only: Optional[tuple] = None,
) -> list:
    docs = list(docs)
    owner_ids = list({getattr(d, field) for d in docs if getattr(d, field) is not None})
    users = await UserExtra.find({"_id": {"$in": owner_ids}}).to_list() if owner_ids else []
    by_id = {str(u.id): u for u in users}

    out = []
    for d in docs:
        item = doc_to_dict(d)
        owner_id = getattr(d, field, None)
        owner = by_id.get(str(owner_id)) if owner_id is not None else None
        if owner is not None:
            owner_dict = doc_to_dict(owner)
            if only is not None:
                keep = set(only) | {"_id", "image"}
                owner_dict = {k: v for k, v in owner_dict.items() if k in keep}
            item[field] = owner_dict
            item["ownerEmail"] = owner.email
            item["ownerName"] = owner.name
            item["ownerImage"] = getattr(owner, "image", None) or None
        else:
            item["ownerEmail"] = None
            item["ownerName"] = None
            item["ownerImage"] = None
        out.append(item)
    return out
