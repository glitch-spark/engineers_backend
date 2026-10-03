"""Invite codes for bidder registration."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bids.invites import (
    clear_invite,
    generate_code,
    hash_code,
    invite_is_live,
    issue_invite,
    normalize_code,
)


def test_code_shape():
    code = generate_code("puma")
    assert re.fullmatch(r"PUMA-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4}-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4}", code)
    assert generate_code("ab")[:5] == "ABXX-" and generate_code("ana-silva")[:5] == "ANAS-"
    assert len({generate_code("puma") for _ in range(200)}) == 200


def test_normalize_forgives_typing():
    assert normalize_code(" puma-7k2q-9xmd ") == "PUMA7K2Q9XMD"
    assert normalize_code("PUMA-7K2Q-9XMO") == normalize_code("puma 7k2q 9xm0")
    assert normalize_code("ILL1") == "1111"
    assert hash_code("puma-7k2q-9xmd") == hash_code("PUMA7K2Q9XMD")


def test_issue_and_live():
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    b = SimpleNamespace(
        folder="puma",
        inviteCodeHash=None,
        inviteExpiresAt=None,
        archivedAt=None,
        registeredAt=None,
    )
    code = issue_invite(b, now)
    assert b.inviteCodeHash == hash_code(code) and b.inviteExpiresAt == now + timedelta(days=7)
    assert invite_is_live(b, now + timedelta(days=6, hours=23))
    assert not invite_is_live(b, now + timedelta(days=7))
    b.inviteExpiresAt = b.inviteExpiresAt.replace(tzinfo=None)  # as read back from Mongo
    assert invite_is_live(b, now)
    b.registeredAt = now
    assert not invite_is_live(b, now)
    clear_invite(b)
    assert b.inviteCodeHash is None and b.inviteExpiresAt is None
