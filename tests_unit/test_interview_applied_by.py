"""Who applied for an interview's job, self or one of the profile owner's bidders: create / edit rules (in-memory
MongoDB via mongomock-motor; routes called directly)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.config import get_settings
from app.models import Account, Interview, InterviewQuestion, UserExtra
from app.models.bidder import Bidder
from app.routers import interviews
from app.routers.interviews import AppliedByIn, InterviewCreate, InterviewUpdate, RoundIn, create_interview, update_interview

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["interview-applied-by-unit"],
                      document_models=[UserExtra, Account, Interview, InterviewQuestion, Bidder])

    async def no_slack(*a, **kw):
        return None

    monkeypatch.setattr(interviews, "_sync_caller_posts", no_slack)


async def _setup():
    ana = await UserExtra(name="Ana", email="ana@x.io", role="staff").insert()
    ben = await UserExtra(name="Ben", email="ben@x.io", role="staff").insert()
    admin = await UserExtra(name="Root", email="root@x.io", role="admin").insert()
    prof = await Account(name="Jordan", createdBy=ana.id).insert()
    eu = await Bidder(createdBy=ana.id, name="Eugine", profileId=prof.id, rate=1, screenshotFolderUrl="s").insert()
    other = await Bidder(createdBy=ben.id, name="Bo", rate=1, screenshotFolderUrl="s").insert()
    return ana, ben, admin, prof, eu, other


def _body(prof, applied=None, **kw):
    return InterviewCreate(accountId=str(prof.id), companyName="Acme",
                           round=RoundIn(stage="intro", scheduledAt="2026-11-05T15:00:00Z"), appliedBy=applied, **kw)


async def test_create_with_a_bidder_saves_its_name(db):
    ana, _ben, _admin, prof, eu, _other = await _setup()
    out = await create_interview(_body(prof, AppliedByIn(kind="bidder", bidderId=str(eu.id))), ana)
    assert out["appliedBy"]["kind"] == "bidder" and str(out["appliedBy"]["bidderId"]) == str(eu.id)
    assert out["appliedBy"]["bidderName"] == "Eugine"
    saved = await Interview.get(out["_id"])
    assert saved.appliedBy.bidderId == eu.id


async def test_create_self_has_no_bidder(db):
    ana, *_ = await _setup()
    prof = await Account.find_one({"name": "Jordan"})
    out = await create_interview(_body(prof, AppliedByIn(kind="self", bidderId="ignored-not-an-id")), ana)
    assert out["appliedBy"]["kind"] == "self" and out["appliedBy"]["bidderId"] is None


async def test_bidder_must_belong_to_the_profile_owner(db):
    ana, _ben, admin, prof, eu, other = await _setup()
    for user, bid in [(ana, str(other.id)), (ana, "not-an-id"), (ana, None)]:
        with pytest.raises(HTTPException) as e:
            await create_interview(_body(prof, AppliedByIn(kind="bidder", bidderId=bid)), user)
        assert e.value.status_code == 422, bid
    # an admin entering it for Ana picks from Ana's bidders
    out = await create_interview(_body(prof, AppliedByIn(kind="bidder", bidderId=str(eu.id))), admin)
    assert out["appliedBy"]["bidderName"] == "Eugine"


async def test_missing_is_allowed_unless_required(db, monkeypatch):
    ana, _ben, _admin, prof, _eu, _other = await _setup()
    assert (await create_interview(_body(prof), ana))["appliedBy"] is None
    settings = get_settings().model_copy(update={"INTERVIEW_APPLIED_BY_REQUIRED": True})
    monkeypatch.setattr(interviews, "get_settings", lambda: settings)
    with pytest.raises(HTTPException) as e:
        await create_interview(_body(prof), ana)
    assert e.value.status_code == 422


async def test_edit_tags_an_old_interview_and_keeps_it_when_absent(db):
    ana, _ben, _admin, prof, eu, _other = await _setup()
    out = await create_interview(_body(prof), ana)
    iv_id = str(out["_id"])
    out = await update_interview(iv_id, InterviewUpdate(appliedBy=AppliedByIn(kind="bidder", bidderId=str(eu.id))), ana)
    assert out["appliedBy"]["bidderName"] == "Eugine"
    out = await update_interview(iv_id, InterviewUpdate(appliedPosition="Backend"), ana)
    assert out["appliedBy"]["bidderName"] == "Eugine"
    out = await update_interview(iv_id, InterviewUpdate(appliedBy=AppliedByIn(kind="self")), ana)
    assert out["appliedBy"]["kind"] == "self"


async def test_archived_bidder_stays_valid_on_edit(db):
    from datetime import datetime

    ana, _ben, _admin, prof, eu, _other = await _setup()
    out = await create_interview(_body(prof, AppliedByIn(kind="bidder", bidderId=str(eu.id))), ana)
    eu.archivedAt = datetime(2026, 11, 6)
    await eu.save()
    out = await update_interview(str(out["_id"]),
                                 InterviewUpdate(appliedBy=AppliedByIn(kind="bidder", bidderId=str(eu.id))), ana)
    assert out["appliedBy"]["bidderName"] == "Eugine"


async def test_options_list_the_profile_owners_active_bidders(db):
    from datetime import datetime

    from app.routers.interviews import applied_by_options

    ana, ben, admin, prof, eu, _other = await _setup()
    other_prof = await Account(name="Sam", createdBy=ana.id).insert()
    await Bidder(createdBy=ana.id, name="Al", profileId=other_prof.id, rate=1, screenshotFolderUrl="s").insert()
    await Bidder(createdBy=ana.id, name="Gone", profileId=prof.id, rate=1, screenshotFolderUrl="s",
                 archivedAt=datetime(2026, 11, 1)).insert()
    for user in (ana, admin):
        out = await applied_by_options(str(prof.id), user)
        assert [(b["name"], b["onProfile"]) for b in out["bidders"]] == [("Al", False), ("Eugine", True)]
        assert out["bidders"][1]["id"] == str(eu.id)
    with pytest.raises(HTTPException) as e:
        await applied_by_options(str(prof.id), ben)
    assert e.value.status_code == 403
