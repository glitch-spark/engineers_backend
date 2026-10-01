"""Daily plan API: a goal set in the morning, a follow-up with what was done."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.models import DailyPlan, Interview

DAY = "2026-10-01"  # Thursday


def _goal_body(**kw):
    body = {
        "goal": {"bidsSelf": 150, "bidsBidder": 100},
        "goalItems": [{"text": "Seek caller"}, {"text": "Bidder review"}],
        "notes": "",
    }
    body.update(kw)
    return body


def _follow_up(**kw):
    body = _goal_body(
        goalItems=[{"text": "Seek caller", "done": True}, {"text": "Bidder review"}],
        done={"bidsSelf": 140, "bidsBidder": 100},
        notes="Bidder review moved to tomorrow.",
    )
    body.update(kw)
    return body


async def test_requires_auth(client):
    assert (await client.get(f"/daily-plans/{DAY}")).status_code == 401


async def test_goal_only_save_does_not_log_or_post(client, staff_user, auth_header):
    with patch("app.routers.daily_plans.sync_daily_plan_message", new=AsyncMock(return_value=None)) as sync:
        r = await client.put(f"/daily-plans/{DAY}", headers=auth_header(staff_user), json=_goal_body())
    assert r.status_code == 200, r.text
    day = r.json()
    assert day["exists"] is True and day["loggedAt"] is None
    assert day["goal"] == {"bidsSelf": 150, "bidsBidder": 100, "interviewsSelf": 0, "interviewsCaller": 0}
    assert [i["text"] for i in day["goalItems"]] == ["Seek caller", "Bidder review"]
    sync.assert_not_called()


async def test_follow_up_logs_and_posts(client, staff_user, auth_header):
    h = auth_header(staff_user)
    await client.put(f"/daily-plans/{DAY}", headers=h, json=_goal_body())
    with patch("app.routers.daily_plans.sync_daily_plan_message", new=AsyncMock(return_value=None)) as sync:
        r = await client.put(f"/daily-plans/{DAY}", headers=h, json=_follow_up())
    assert r.status_code == 200, r.text
    day = r.json()
    assert day["loggedAt"] is not None
    assert day["done"]["bidsSelf"] == 140 and day["done"]["bidsBidder"] == 100
    assert [i["done"] for i in day["goalItems"]] == [True, False]
    assert day["notes"] == "Bidder review moved to tomorrow."
    sync.assert_awaited_once()

    stored = await DailyPlan.find_one({"userId": staff_user.id})
    assert (stored.year, stored.weekNumber) == (2026, 40)
    first_logged = stored.loggedAt
    await client.put(f"/daily-plans/{DAY}", headers=h, json=_follow_up(notes="edited"))
    stored = await DailyPlan.find_one({"userId": staff_user.id})
    assert stored.loggedAt == first_logged and stored.notes == "edited"


async def test_interviews_come_from_records_and_typed_values_are_ignored(client, staff_user, auth_header):
    account_id = PydanticObjectId()
    await Interview(accountId=account_id, createdBy=staff_user.id, scheduledAt=datetime(2026, 10, 1, 15),
                    stage="intro", status="completed").insert()
    await Interview(accountId=account_id, createdBy=staff_user.id, scheduledAt=datetime(2026, 10, 1, 18),
                    stage="tech_round_1", status="scheduled",
                    caller={"enabled": True, "startsAt": datetime(2026, 10, 1, 18)}).insert()
    await Interview(accountId=account_id, createdBy=staff_user.id, scheduledAt=datetime(2026, 10, 1, 19),
                    stage="panel", status="canceled").insert()

    h = auth_header(staff_user)
    day = (await client.get(f"/daily-plans/{DAY}", headers=h)).json()
    assert day["exists"] is False
    assert day["interviews"] == {"self": 1, "caller": 1, "stages": {"self": {"intro": 1}, "caller": {"tech": 1}}}
    assert (day["done"]["interviewsSelf"], day["done"]["interviewsCaller"]) == (1, 1)

    body = _follow_up(done={"bidsSelf": 1, "bidsBidder": 0, "interviewsSelf": 9})
    r = await client.put(f"/daily-plans/{DAY}", headers=h, json=body)
    assert r.json()["done"]["interviewsSelf"] == 1


async def test_week_list_has_templates_for_missing_days(client, staff_user, auth_header):
    h = auth_header(staff_user)
    await client.put("/daily-plans/2026-09-29", headers=h, json=_follow_up())
    await client.put("/daily-plans/2026-10-04", headers=h, json=_follow_up())  # Sunday entry
    r = await client.get("/daily-plans?weekStart=2026-09-28", headers=h)
    assert r.status_code == 200
    days = r.json()["days"]
    assert [d["date"] for d in days] == [
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    ]
    assert [d["exists"] for d in days] == [False, True, False, False, False, False, True]


async def test_week_list_without_sunday_entry_has_six_days(client, staff_user, auth_header):
    r = await client.get("/daily-plans?weekStart=2026-09-28", headers=auth_header(staff_user))
    assert len(r.json()["days"]) == 6


async def test_delete_day(client, staff_user, auth_header):
    h = auth_header(staff_user)
    await client.put(f"/daily-plans/{DAY}", headers=h, json=_goal_body())
    assert (await client.delete(f"/daily-plans/{DAY}", headers=h)).status_code == 200
    assert await DailyPlan.find_one({"userId": staff_user.id}) is None
    assert (await client.delete(f"/daily-plans/{DAY}", headers=h)).status_code == 404


@pytest.mark.parametrize("path,body", [
    (f"/daily-plans/{DAY}", {"goal": {"bidsSelf": -1}}),
    (f"/daily-plans/{DAY}", {"done": {"bidsBidder": -2}}),
    (f"/daily-plans/{DAY}", {"goalItems": [{"text": f"item {n}"} for n in range(21)]}),
    (f"/daily-plans/{DAY}", {"goalItems": [{"text": "x" * 201}]}),
    (f"/daily-plans/{DAY}", {"notes": "x" * 2001}),
    ("/daily-plans/2026-13-01", {}),
    ("/daily-plans/20261001", {}),
])
async def test_put_validation_422(client, staff_user, auth_header, path, body):
    r = await client.put(path, headers=auth_header(staff_user), json=body)
    assert r.status_code == 422, r.text


async def test_week_list_requires_monday(client, staff_user, auth_header):
    r = await client.get("/daily-plans?weekStart=2026-09-29", headers=auth_header(staff_user))
    assert r.status_code == 422


async def test_permissions_matrix(client, staff_user, staff_user_2, admin_user, auth_header):
    await client.put(f"/daily-plans/{DAY}", headers=auth_header(staff_user), json=_follow_up())
    other = f"/daily-plans/{DAY}?userId={staff_user.id}"
    assert (await client.put(f"/daily-plans/{DAY}", headers=auth_header(admin_user), json=_goal_body())).status_code == 403
    assert (await client.delete(f"/daily-plans/{DAY}", headers=auth_header(admin_user))).status_code == 403
    assert (await client.get(other, headers=auth_header(staff_user_2))).status_code == 403
    r = await client.get(other, headers=auth_header(admin_user))
    assert r.status_code == 200 and r.json()["done"]["bidsSelf"] == 140
    assert (await client.get(f"/daily-plans/{DAY}?userId={staff_user.id}", headers=auth_header(staff_user))).status_code == 200
    assert (await client.get(f"/daily-plans/{DAY}?userId=000000000000000000000000",
                             headers=auth_header(admin_user))).status_code == 404
    assert (await client.get(f"/daily-plans/{DAY}?userId=nope", headers=auth_header(admin_user))).status_code == 400


async def test_legacy_doc_readable(client, staff_user, auth_header):
    await DailyPlan.get_motor_collection().insert_one({
        "userId": staff_user.id,
        "date": datetime(2026, 10, 1),
        "year": 2026,
        "weekNumber": 40,
        "planType": "regular",
        "bidsHandsOn": 7,
        "bidsByBidder": 3,
        "interviewsDone": 2,
        "todayItems": ["Old item"],
        "createdAt": datetime(2026, 10, 1, 21),
        "updatedAt": datetime(2026, 10, 1, 21),
    })
    day = (await client.get(f"/daily-plans/{DAY}", headers=auth_header(staff_user))).json()
    assert day["exists"] is True
    assert day["done"]["bidsSelf"] == 7 and day["done"]["bidsBidder"] == 3
    assert day["done"]["interviewsSelf"] == 0  # old typed count dropped; no interview records
    assert day["goalItems"] == [{"text": "Old item", "done": True}]
    assert day["loggedAt"] is not None
