"""Weekly plan Goal vs Done API."""

from datetime import datetime

from beanie import PydanticObjectId

from app.models import Interview, WeeklyPlan
from app.models.interview import InterviewCaller

WEEK = "2026-09-28"


def _week_body(**kw):
    body = {
        "goal": {"bidsSelf": 30, "bidsBidder": 20, "interviewsSelf": 4, "interviewsCaller": 1,
                 "profiles": {"US": 4}, "linkedin": {"US": 2}},
        "goalItems": [{"text": "Refresh resumes"}, {"text": "Prep panel", "done": True}],
        "planNotes": "Focus on US",
        "recapNotes": "",
        "doneOverrides": {},
    }
    body.update(kw)
    return body


def _day_body(self_=0, bidder=0, linkedin=None):
    return {"goal": {}, "done": {"bidsSelf": self_, "bidsBidder": bidder, "linkedin": linkedin or {}}}


async def test_requires_auth(client):
    assert (await client.get(f"/weekly-plans/{WEEK}")).status_code == 401


async def test_put_and_get_week(client, staff_user, auth_header):
    h = auth_header(staff_user)
    r = await client.put(f"/weekly-plans/{WEEK}", headers=h, json=_week_body())
    assert r.status_code == 200, r.text
    week = r.json()
    assert week["weekStart"] == WEEK and week["exists"] is True
    assert week["goal"]["bidsSelf"] == 30 and week["goal"]["profiles"] == {"US": 4}
    assert [i["done"] for i in week["goalItems"]] == [False, True]
    assert week["planNotes"] == "Focus on US"

    stored = await WeeklyPlan.find_one({"userId": staff_user.id})
    assert stored.weekStart == datetime(2026, 9, 28)
    assert stored.startDate == datetime(2026, 9, 28)
    assert stored.endDate == datetime(2026, 10, 4, 23, 59, 59, 999000)
    assert (stored.year, stored.weekNumber) == (2026, 40)

    # A second save updates the same document.
    await client.put(f"/weekly-plans/{WEEK}", headers=h, json=_week_body(recapNotes="Done"))
    assert await WeeklyPlan.find({"userId": staff_user.id}).count() == 1
    r = await client.get(f"/weekly-plans/{WEEK}", headers=h)
    assert r.json()["recapNotes"] == "Done"


async def test_empty_week_template(client, staff_user, auth_header):
    week = (await client.get(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user))).json()
    assert week["exists"] is False
    assert week["goal"]["bidsSelf"] == 0
    assert [d["date"] for d in week["days"]] == [
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03",
    ]


async def test_week_done_auto_from_days_and_interviews(client, staff_user, auth_header):
    h = auth_header(staff_user)
    await client.put("/daily-plans/2026-09-28", headers=h, json=_day_body(4, 1, {"US": 1}))
    await client.put("/daily-plans/2026-09-30", headers=h, json=_day_body(6, 2, {"US": 2}))
    await client.put("/daily-plans/2026-10-04", headers=h, json=_day_body(1, 0, {"US": 3}))  # Sunday
    account_id = PydanticObjectId()
    await Interview(accountId=account_id, createdBy=staff_user.id, scheduledAt=datetime(2026, 9, 29, 15),
                    stage="intro", status="scheduled").insert()
    await Interview(accountId=account_id, createdBy=staff_user.id, scheduledAt=datetime(2026, 10, 1, 15),
                    stage="final", status="scheduled",
                    caller=InterviewCaller(enabled=True, startsAt=datetime(2026, 10, 1, 15))).insert()

    week = (await client.get(f"/weekly-plans/{WEEK}", headers=h)).json()
    assert week["done"]["bidsSelf"] == 11 and week["done"]["bidsBidder"] == 3
    assert week["done"]["interviewsSelf"] == 1 and week["done"]["interviewsCaller"] == 1
    assert week["done"]["linkedin"] == {"US": 3}  # latest logged day (Sunday)
    assert week["stages"] == {"self": {"intro": 1}, "caller": {"final": 1}}
    days = {d["date"]: d for d in week["days"]}
    assert "2026-10-04" in days
    assert days["2026-09-28"]["logged"] is True and days["2026-09-29"]["logged"] is False
    assert days["2026-09-30"]["done"]["bidsSelf"] == 6


async def test_week_override_and_reset(client, staff_user, auth_header):
    h = auth_header(staff_user)
    await client.put("/daily-plans/2026-09-28", headers=h, json=_day_body(4, 1))
    r = await client.put(f"/weekly-plans/{WEEK}", headers=h, json=_week_body(doneOverrides={"bidsSelf": 9}))
    assert r.json()["done"]["bidsSelf"] == 9
    assert r.json()["auto"]["bidsSelf"] == 4
    r = await client.put(f"/weekly-plans/{WEEK}", headers=h, json=_week_body(doneOverrides={"bidsSelf": None}))
    assert r.json()["done"]["bidsSelf"] == 4


async def test_previous_goals_carries_unticked_items(client, staff_user, auth_header):
    h = auth_header(staff_user)
    await client.put("/weekly-plans/2026-09-21", headers=h, json=_week_body())
    r = await client.get(f"/weekly-plans/{WEEK}/previous-goals", headers=h)
    assert r.status_code == 200
    assert r.json()["goal"]["bidsSelf"] == 30
    assert [i["text"] for i in r.json()["goalItems"]] == ["Refresh resumes"]
    assert all(i["done"] is False for i in r.json()["goalItems"])

    r = await client.get("/weekly-plans/2026-09-14/previous-goals", headers=h)
    assert r.json() == {"goal": None, "goalItems": []}


async def test_delete_week(client, staff_user, auth_header):
    h = auth_header(staff_user)
    await client.put(f"/weekly-plans/{WEEK}", headers=h, json=_week_body())
    assert (await client.delete(f"/weekly-plans/{WEEK}", headers=h)).status_code == 200
    assert await WeeklyPlan.find_one({"userId": staff_user.id}) is None
    assert (await client.delete(f"/weekly-plans/{WEEK}", headers=h)).status_code == 404


async def test_week_permissions_matrix(client, staff_user, staff_user_2, admin_user, auth_header):
    await client.put(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user), json=_week_body())
    other = f"/weekly-plans/{WEEK}?userId={staff_user.id}"
    assert (await client.put(f"/weekly-plans/{WEEK}", headers=auth_header(admin_user),
                             json=_week_body())).status_code == 403
    assert (await client.delete(f"/weekly-plans/{WEEK}", headers=auth_header(admin_user))).status_code == 403
    assert (await client.get(other, headers=auth_header(staff_user_2))).status_code == 403
    r = await client.get(other, headers=auth_header(admin_user))
    assert r.status_code == 200 and r.json()["goal"]["bidsSelf"] == 30


async def test_week_validation_422(client, staff_user, auth_header):
    h = auth_header(staff_user)
    assert (await client.get("/weekly-plans/2026-09-29", headers=h)).status_code == 422
    assert (await client.put("/weekly-plans/2026-09-29", headers=h, json=_week_body())).status_code == 422
    assert (await client.put(f"/weekly-plans/{WEEK}", headers=h,
                             json=_week_body(planNotes="x" * 4001))).status_code == 422
    assert (await client.put(f"/weekly-plans/{WEEK}", headers=h,
                             json=_week_body(goal={"profiles": {"Mars": 1}}))).status_code == 422


async def test_legacy_weekly_doc_readable_by_weekStart(client, staff_user, auth_header):
    await WeeklyPlan.get_motor_collection().insert_one({
        "userId": staff_user.id,
        "weekNumber": 40,
        "year": 2026,
        "startDate": datetime(2026, 9, 28, 4, 0),
        "endDate": datetime(2026, 10, 5, 3, 59),
        "metrics": [{"key": "bids", "label": "Job applies", "target": 40, "actual": 12},
                    {"key": "interviews", "label": "Interviews", "target": 5, "actual": 1}],
        "status": "planned",
        "content": "Old plan",
        "result": "Old recap",
        "createdAt": datetime(2026, 9, 28),
        "updatedAt": datetime(2026, 9, 28),
    })
    week = (await client.get(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user))).json()
    assert week["exists"] is True
    assert week["goal"]["bidsSelf"] == 40 and week["goal"]["interviewsSelf"] == 5
    assert week["planNotes"] == "Old plan" and week["recapNotes"] == "Old recap"

    # Saving it converts the document in place (no duplicate).
    await client.put(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user), json=_week_body())
    assert await WeeklyPlan.find({"userId": staff_user.id}).count() == 1
