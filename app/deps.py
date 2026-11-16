from typing import Annotated, Any, NamedTuple, Optional

from beanie import PydanticObjectId
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

from app.models import Account, UserExtra
from app.models.bidder import Bidder
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)],
) -> UserExtra:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    try:
        payload = decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    if payload.get("typ") in ("bidder", "ext-owner"):  # extension tokens never act as app users
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token payload")
    try:
        user_id = PydanticObjectId(sub)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token subject")

    user = await UserExtra.get(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user


async def require_admin(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> UserExtra:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user


class ExtOwnerAuth(NamedTuple):
    user: UserExtra
    account: Account  # the profile this sign-in records for
    claims: dict[str, Any]


async def get_current_ext_owner(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)],
) -> ExtOwnerAuth:
    """An owner's extension sign-in: still active, not an admin, and the profile still theirs and not archived. One
    message for every failure."""
    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise invalid
    try:
        payload = decode_token(credentials.credentials)
    except JWTError:
        raise invalid
    if payload.get("typ") != "ext-owner":
        raise invalid
    try:
        user_id, profile_id = PydanticObjectId(payload.get("sub")), PydanticObjectId(payload.get("pid"))
    except Exception:
        raise invalid
    user = await UserExtra.get(user_id)
    account = await Account.get(profile_id)
    if (user is None or not user.isActive or user.role == "admin" or account is None or account.archived
            or account.createdBy != user.id):
        raise invalid
    return ExtOwnerAuth(user, account, payload)


class BidderAuth(NamedTuple):
    bidder: Bidder
    claims: dict[str, Any]  # the presented token's, e.g. iat for renewal


async def get_current_bidder(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)],
) -> BidderAuth:
    """Extension auth: one message for every failure so a caller learns nothing about why."""
    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise invalid
    try:
        payload = decode_token(credentials.credentials)
    except JWTError:
        raise invalid
    if payload.get("typ") != "bidder":
        raise invalid
    try:
        bidder_id = PydanticObjectId(payload.get("sub"))
    except Exception:
        raise invalid
    bidder = await Bidder.get(bidder_id)
    if (
        bidder is None
        or bidder.archivedAt is not None
        or bidder.registeredAt is None
        or payload.get("ver") != bidder.tokenVersion
    ):
        raise invalid
    return BidderAuth(bidder, payload)
