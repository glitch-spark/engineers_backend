"""Symmetric encryption for OAuth refresh tokens at rest.

Refresh tokens are long-lived bearer credentials that grant access to a
user's gmail without further auth — leakage = inbox exposure. We store
them encrypted with Fernet (AES128-CBC + HMAC) keyed off
`EMAIL_TOKEN_KEY` so a database dump alone is not enough to read mail.

Key rotation strategy when needed: add EMAIL_TOKEN_KEY_OLD, attempt
decrypt with new first then old, re-encrypt on successful decrypt. Not
implemented yet — single key is fine until first rotation.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class EmailCryptoUnavailable(RuntimeError):
    """Raised when EMAIL_TOKEN_KEY is missing — the gmail integration
    routes refuse to start the OAuth flow in that case."""


@lru_cache
def _cipher() -> Fernet:
    key = get_settings().EMAIL_TOKEN_KEY
    if not key:
        raise EmailCryptoUnavailable(
            "EMAIL_TOKEN_KEY is not configured — gmail integration disabled. "
            "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise EmailCryptoUnavailable("refresh token failed to decrypt — key changed?") from exc
