"""Daily plan Goal vs Done API."""

from datetime import datetime

import pytest
from beanie import PydanticObjectId

from app.models import Account, DailyPlan, Interview
from app.models.interview import InterviewCaller

DAY = "2026-10-01"  # Thursday


def _body(**kw):
    body = {
        "goal": {"bidsSelf": 6, "bidsBidder": 4, "interviewsSelf": 1, "interviewsCaller": 1, "linkedin": {"US": 2}},
        "goalItems": [{"text": "Fix resume", "done": True}, {"text": "Call recruiter"}],
        "done": {"bidsSelf": 5, "bidsBidder": 4, "linkedin": {"US": 2}},
        "doneOverrides": {},
        "notes": "Slow day",
    }
    body.update(kw)
    return body


async def test_requires_auth(client):
    assert (await client.get(f"/daily-plans/{DAY}")).status_code == 401


async def test_put_day_creates_and_logs(client, staff_user, auth_header):
    r = await client.put(f"/daily-plans/{DAY}", headers=auth_header(staff_user), json=_body())
    assert r.status_code == 200, r.text
    day = r.json()["day"]
    assert day["date"] == DAY and day["exists"] is True
    assert day["goal"]["bidsSelf"] == 6 and day["goal"]["linkedin"] == {"US": 2}
    assert day["done"]["bidsSelf"] == 5 and day["done"]["bidsBidder"] == 4
    assert [i["done"] for i in day["goalItems"]] == [True, False]
    assert day["notes"] == "Slow day"
    assert day["loggedAt"] is not None
    assert r.json()["tomorrow"] is None

    stored = await DailyPlan.find_one({"userId": staff_user.id})
    assert stored.year == 2026 and stored.weekNumber == 40

    r = await client.get(f"/daily-plans/{DAY}", headers=auth_header(staff_user))
    assert r.json()["done"]["bidsSelf"] == 5


async def test_goal_only_put_does_not_log(client, staff_user, auth_header):
    body = _body()
    del body["done"]
    r = await client.put("/daily-plans/2026-10-02", headers=auth_header(staff_user), json=body)
    assert r.status_code == 200
    assert r.json()["day"]["loggedAt"] is None


async def test_put_day_with_tomorrow_sets_next_working_day_goal_only(client, staff_user, auth_header):
    # Monday already has Done + notes; Saturday's "tomorrow" must only touch its goal.
    await client.put("/daily-plans/2026-10-05", headers=auth_header(staff_user), json=_body(notes="keep me"))
    monday = await DailyPlan.find_one({"userId": staff_user.id})
    logged_at = monday.loggedAt

    body = _body(tomorrow={"goal": {"bidsSelf": 9}, "goalItems": [{"text": "Prep panel"}]})
    r = await client.put("/daily-plans/2026-10-03", headers=auth_header(staff_user), json=body)
    assert r.status_code == 200, r.text
    tomorrow = r.json()["tomorrow"]
    assert tomorrow["date"] == "2026-10-05"
    assert tomorrow["goal"]["bidsSelf"] == 9
    assert [i["text"] for i in tomorrow["goalItems"]] == ["Prep panel"]
    assert tomorrow["done"]["bidsSelf"] == 5
    assert tomorrow["notes"] == "keep me"
    stored = await DailyPlan.find_one({"userId": staff_user.id, "date": datetime(2026, 10, 5)})
    assert stored.loggedAt == logged_at


async def test_tomorrow_creates_goal_only_entry(client, staff_user, auth_header):
    body = _body(tomorrow={"goal": {"bidsSelf": 3}, "goalItems": []})
    await client.put(f"/daily-plans/{DAY}", headers=auth_header(staff_user), json=body)
    fri = await DailyPlan.find_one({"userId": staff_user.id, "date": datetime(2026, 10, 2)})
    assert fri is not None and fri.goal.bidsSelf == 3 and fri.loggedAt is None


async def test_auto_interviews_on_day(client, staff_user, auth_header):
    account_id = PydanticObjectId()
    await Interview(accountId=account_id, createdBy=staff_user.id, scheduledAt=datetime(2026, 10, 1, 15),
                    stage="intro", status="scheduled").insert()
    await Interview(accountId=account_id, createdBy=staff_user.id, scheduledAt=datetime(2026, 10, 1, 18),
                    stage="tech_round_1", status="scheduled",
                    caller=InterviewCaller(enabled=True, startsAt=datetime(2026, 10, 1, 18))).insert()

    day = (await client.get(f"/daily-plans/{DAY}", headers=auth_header(staff_user))).json()
    assert day["exists"] is False
    assert day["auto"]["interviewsSelf"] == 1 and day["auto"]["interviewsCaller"] == 1
    assert day["auto"]["stages"] == {"self": {"intro": 1}, "caller": {"tech": 1}}
    assert day["done"]["interviewsSelf"] == 1 and day["done"]["interviewsCaller"] == 1


async def test_override_then_null_restores_auto(client, staff_user, auth_header):
    await Interview(accountId=PydanticObjectId(), createdBy=staff_user.id,
                    scheduledAt=datetime(2026, 10, 1, 15), stage="intro", status="scheduled").insert()
    h = auth_header(staff_user)
    r = await client.put(f"/daily-plans/{DAY}", headers=h, json=_body(doneOverrides={"interviewsSelf": 4}))
    assert r.json()["day"]["done"]["interviewsSelf"] == 4
    assert r.json()["day"]["auto"]["interviewsSelf"] == 1
    r = await client.put(f"/daily-plans/{DAY}", headers=h, json=_body(doneOverrides={"interviewsSelf": None}))
    assert r.json()["day"]["done"]["interviewsSelf"] == 1


async def test_profiles_snapshot_on_log(client, staff_user, auth_header):
    await Account(name="a", region="US", createdBy=staff_user.id).insert()
    await Account(name="b", region="EU", createdBy=staff_user.id).insert()
    h = auth_header(staff_user)
    r = await client.put(f"/daily-plans/{DAY}", headers=h, json=_body())
    assert r.json()["day"]["done"]["profiles"] == {"US": 1, "EU": 1}
    await Account(name="c", region="US", createdBy=staff_user.id).insert()
    day = (await client.get(f"/daily-plans/{DAY}", headers=h)).json()
    assert day["done"]["profiles"] == {"US": 1, "EU": 1}  # snapshot, not live
    assert day["auto"]["profiles"] == {"US": 2, "EU": 1}


async def test_week_list_has_templates_for_missing_days(client, staff_user, auth_header):
    h = auth_header(staff_user)
    await client.put("/daily-plans/2026-09-29", headers=h, json=_body())
    await client.put("/daily-plans/2026-10-04", headers=h, json=_body())  # Sunday entry
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
    await client.put(f"/daily-plans/{DAY}", headers=h, json=_body())
    assert (await client.delete(f"/daily-plans/{DAY}", headers=h)).status_code == 200
    assert await DailyPlan.find_one({"userId": staff_user.id}) is None
    assert (await client.delete(f"/daily-plans/{DAY}", headers=h)).status_code == 404


@pytest.mark.parametrize("path,body", [
    (f"/daily-plans/{DAY}", {"goal": {"linkedin": {"Mars": 1}}}),
    (f"/daily-plans/{DAY}", {"goal": {"bidsSelf": -1}}),
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
    await client.put(f"/daily-plans/{DAY}", headers=auth_header(staff_user), json=_body())
    other = f"/daily-plans/{DAY}?userId={staff_user.id}"
    assert (await client.put(f"/daily-plans/{DAY}", headers=auth_header(admin_user), json=_body())).status_code == 403
    assert (await client.delete(f"/daily-plans/{DAY}", headers=auth_header(admin_user))).status_code == 403
    assert (await client.get(other, headers=auth_header(staff_user_2))).status_code == 403
    r = await client.get(other, headers=auth_header(admin_user))
    assert r.status_code == 200 and r.json()["done"]["bidsSelf"] == 5
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
    assert day["done"]["interviewsSelf"] == 2
    assert day["goalItems"] == [{"text": "Old item", "done": True}]
    assert day["loggedAt"] is not None
