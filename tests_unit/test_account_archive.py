"""Unit tests for profile (account) archiving filters (no database)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.models.account import Account  # noqa: E402
from app.routers.accounts import AccountUpdate, archive_status_query  # noqa: E402


def test_active_is_default_and_includes_rows_without_the_field():
    # Existing profiles have no `archived` field: they must count as active.
    assert archive_status_query("") == {"archived": {"$ne": True}}
    assert archive_status_query("active") == {"archived": {"$ne": True}}


def test_archived_and_all():
    assert archive_status_query("archived") == {"archived": True}
    assert archive_status_query("all") == {}


def test_unknown_status_is_rejected():
    with pytest.raises(HTTPException):
        archive_status_query("deleted")


def test_model_and_update_accept_archived():
    assert Account.model_fields["archived"].default is False
    assert AccountUpdate(archived=True).archived is True
