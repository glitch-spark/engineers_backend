import re
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.models import UserExtra
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    username: str
    email: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    role: str
    image: Optional[str] = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


def _user_to_out(u: UserExtra) -> UserOut:
    return UserOut(
        id=str(u.id), name=u.name, email=u.email, role=u.role,
        image=getattr(u, "image", None) or None,
    )


def _ci_eq(field: str, value: str) -> dict:
    # Escape regex specials so user input cannot be interpreted as a pattern.
    return {field: {"$regex": f"^{re.escape(value)}$", "$options": "i"}}


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn) -> dict:
    if not body.username or not body.email or not body.password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing fields")

    email_lower = body.email.lower()
    existing = await UserExtra.find_one(
        {"$or": [_ci_eq("name", body.username), _ci_eq("email", email_lower)]}
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Try another username or email",
        )

    count = await UserExtra.count()
    is_first = count == 0
    role = "admin" if is_first else "staff"

    user = UserExtra(
        name=body.username,
        email=email_lower,
        hashedPassword=hash_password(body.password),
        role=role,
        isActive=is_first,  # bootstrap admin auto-active; everyone else awaits approval
        # Default-on for staff so the leaderboard is populated. Admins
        # default-off (workspace owners shouldn't auto-display on rankings).
        leaderboardOptIn=not is_first,
    )
    await user.insert()
    return {"ok": True, "pendingApproval": not is_first}


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn) -> TokenOut:
    username = body.username.lower()
    if not username or not body.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    user = await UserExtra.find_one(
        {"$or": [_ci_eq("name", username), _ci_eq("email", username)]}
    )
    if user is None or not user.hashedPassword:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    if not verify_password(body.password, user.hashedPassword):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    if not user.isActive:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account pending admin approval",
        )

    token = create_access_token(sub=str(user.id), role=user.role)
    return TokenOut(access_token=token, token_type="bearer", user=_user_to_out(user))


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[UserExtra, Depends(get_current_user)]) -> UserOut:
    return _user_to_out(user)
