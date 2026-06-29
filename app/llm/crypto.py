from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

_ENC_PREFIX = "enc:"


def _fernet(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(plain: str, secret: str) -> str:
    if not plain:
        return ""
    token = _fernet(secret).encrypt(plain.encode("utf-8")).decode("ascii")
    return f"{_ENC_PREFIX}{token}"


def decrypt_secret(stored: str, secret: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        return stored
    token = stored[len(_ENC_PREFIX) :]
    try:
        return _fernet(secret).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ""


def mask_secret(value: str) -> str:
    if not value:
        return ""
    plain = value[len(_ENC_PREFIX) :] if value.startswith(_ENC_PREFIX) else value
    if len(plain) <= 10:
        return "••••••••"
    return f"{plain[:6]}…{plain[-4:]}"
