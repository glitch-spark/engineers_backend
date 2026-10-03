import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest
from beanie import PydanticObjectId
from fastapi import HTTPException

from app.routers.bidders import can_select_profile, ensure_owner

ME, OTHER = PydanticObjectId(), PydanticObjectId()
user = SimpleNamespace(id=ME, role="staff")
admin = SimpleNamespace(id=OTHER, role="admin")


def test_ensure_owner():
    mine = SimpleNamespace(createdBy=ME)
    assert ensure_owner(mine, user) is mine
    for b, u in [(None, user), (SimpleNamespace(createdBy=OTHER), user), (mine, admin)]:
        with pytest.raises(HTTPException) as exc:
            ensure_owner(b, u)
        assert exc.value.status_code == 404


def test_can_select_profile():
    own = SimpleNamespace(createdBy=ME, archived=False)
    assert can_select_profile(own, user)
    assert not can_select_profile(None, user)
    assert not can_select_profile(SimpleNamespace(createdBy=ME, archived=True), user)
    assert not can_select_profile(SimpleNamespace(createdBy=OTHER, archived=False), user)
    assert can_select_profile(SimpleNamespace(createdBy=ME, archived=False), admin)
