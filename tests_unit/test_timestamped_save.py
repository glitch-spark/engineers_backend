"""updatedAt must move when a loaded document is edited and saved (Interview edits went unstamped)."""

from datetime import datetime

from beanie import init_beanie
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.models import Interview


async def test_interview_save_bumps_updated_at():
    db = AsyncMongoMockClient()["ts-unit"]
    await init_beanie(database=db, document_models=[Interview])
    old, _id = datetime(2026, 11, 13), ObjectId()
    start = datetime(2026, 11, 18, 17, 30)
    await db.interviews.insert_one({
        "_id": _id, "createdBy": ObjectId(), "accountId": ObjectId(), "companyName": "A", "stage": "intro",
        "status": "scheduled", "scheduledAt": start, "createdAt": old, "updatedAt": old,
        "stageHistory": [{"id": "r1", "stage": "intro", "at": old, "scheduledAt": start, "status": "scheduled"}],
    })
    iv = await Interview.get(_id)
    iv.stageHistory[0].scheduledAt = datetime(2026, 11, 20, 17, 30)
    await iv.save()
    assert (await db.interviews.find_one({"_id": _id}))["updatedAt"] > old
