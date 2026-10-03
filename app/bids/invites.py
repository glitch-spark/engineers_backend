"""Invite codes for bidder registration."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any


ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
INVITE_TTL = timedelta(days=7)


def generate_code(folder: str) -> str:
    """Generate an invite code in format TAG-XXXX-XXXX.

    TAG is the first 4 characters of folder (keeping only [A-Z0-9]), upper-cased, padded with X to 4.
    XXXX groups are 4 random characters from ALPHABET.
    """
    # Extract the first 4 characters of folder after filtering to [A-Z0-9]
    tag_chars = re.findall(r"[A-Za-z0-9]", folder.upper())
    tag = "".join(tag_chars[:4]).ljust(4, "X")

    # Generate two groups of 4 random characters from ALPHABET
    group1 = "".join(secrets.choice(ALPHABET) for _ in range(4))
    group2 = "".join(secrets.choice(ALPHABET) for _ in range(4))

    return f"{tag}-{group1}-{group2}"


def normalize_code(code: str) -> str:
    """Normalize an invite code for hashing.

    Upper-case, replace O→0, I→1, L→1, then drop everything outside [0-9A-Z]: spaces, dashes, and the tabs,
    NBSPs and zero-width spaces a code picks up when pasted from a chat app.
    """
    normalized = code.upper().replace("O", "0").replace("I", "1").replace("L", "1")
    return re.sub(r"[^0-9A-Z]", "", normalized)


def hash_code(code: str) -> str:
    """Hash a code using SHA256 of its normalized form."""
    normalized = normalize_code(code)
    return hashlib.sha256(normalized.encode()).hexdigest()


def issue_invite(bidder: Any, now: datetime) -> str:
    """Issue an invite code for a bidder.

    Sets bidder.inviteCodeHash and bidder.inviteExpiresAt.
    Returns the code (does not save the bidder).
    """
    code = generate_code(bidder.folder)
    bidder.inviteCodeHash = hash_code(code)
    bidder.inviteExpiresAt = now + INVITE_TTL
    return code


def clear_invite(bidder: Any) -> None:
    """Clear invite code and expiration from a bidder."""
    bidder.inviteCodeHash = None
    bidder.inviteExpiresAt = None


def invite_is_live(bidder: Any, now: datetime) -> bool:
    """Check if an invite code is currently valid and usable.

    Returns False if:
    - No hash is set
    - Already registered
    - Already archived
    - Expired (now >= inviteExpiresAt)

    Treats naive inviteExpiresAt from MongoDB as UTC.
    """
    # Must have a hash
    if bidder.inviteCodeHash is None:
        return False

    # Must not be archived or registered
    if bidder.archivedAt is not None:
        return False
    if bidder.registeredAt is not None:
        return False

    # Must not be expired
    expires_at = bidder.inviteExpiresAt
    if expires_at is None:
        return False

    # Treat naive expiration as UTC
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    # Ensure now is timezone-aware for comparison
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    return now < expires_at
