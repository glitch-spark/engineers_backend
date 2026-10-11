import re
from datetime import datetime
from typing import Iterable, Optional

from beanie import Document, PydanticObjectId
from fastapi import HTTPException, status

from app.model_catalog import ModelNotAvailable, Task, validate_requested
from app.models import Account, UserExtra


def to_object_id(value: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(value)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid id")


def ci_regex(value: str) -> dict:
    return {"$regex": re.escape(value), "$options": "i"}


async def load_owned_account(account_id: str, user: UserExtra) -> Account:
    """The profile, when it exists and `user` owns it (admins see every profile); otherwise 404."""
    account = await Account.get(to_object_id(account_id))
    is_admin = user.role == "admin"
    if account is None or (not is_admin and str(account.createdBy) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return account


# Never serialized, whatever route the document goes out through.
_SECRET_FIELDS = ("hashedPassword", "freeLlmApiKey")
# What an embedded owner/payer carries by default: enough to show who it is, nothing private.
_OWNER_FIELDS = ("_id", "name", "email", "image")


def doc_to_dict(doc: Document) -> dict:
    payload = doc.model_dump(by_alias=True, mode="json")
    for key in _SECRET_FIELDS:
        payload.pop(key, None)
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
    extra: Optional[tuple[tuple[str, str], ...]] = None,
) -> list:
    """Populate `field` as the owner (ownerEmail/Name/Image) and optionally
    extra `(id_field, prefix)` pairs — e.g. `(("payerId", "payer"),)` adds
    payerEmail/payerName/payerImage and replaces that id with the user dict.
    The embedded user dict holds only _id/name/email/image plus any `only` fields.
    """
    docs = list(docs)
    extra = extra or ()
    user_ids: list = []
    seen: set[str] = set()
    for d in docs:
        for id_field, _ in ((field, "owner"), *extra):
            value = getattr(d, id_field, None)
            if value is None:
                continue
            key = str(value)
            if key not in seen:
                seen.add(key)
                user_ids.append(value)
    users = await UserExtra.find({"_id": {"$in": user_ids}}).to_list() if user_ids else []
    by_id = {str(u.id): u for u in users}

    def _attach(item: dict, d: Document, id_field: str, prefix: str) -> None:
        uid = getattr(d, id_field, None)
        person = by_id.get(str(uid)) if uid is not None else None
        if person is not None:
            person_dict = doc_to_dict(person)
            keep = set(_OWNER_FIELDS) | set(only or ())
            person_dict = {k: v for k, v in person_dict.items() if k in keep}
            item[id_field] = person_dict
            item[f"{prefix}Email"] = person.email
            item[f"{prefix}Name"] = person.name
            item[f"{prefix}Image"] = getattr(person, "image", None) or None
        else:
            item[f"{prefix}Email"] = None
            item[f"{prefix}Name"] = None
            item[f"{prefix}Image"] = None

    out = []
    for d in docs:
        item = doc_to_dict(d)
        _attach(item, d, field, "owner")
        for id_field, prefix in extra:
            _attach(item, d, id_field, prefix)
        out.append(item)
    return out


def require_model(task: Task, model_id: Optional[str], user: Optional[UserExtra]) -> Optional[str]:
    """Validate a client-supplied model id; blank means "let the server choose" (returns None)."""
    try:
        spec = validate_requested(task, model_id, user)
    except ModelNotAvailable as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return spec.id if spec else None
