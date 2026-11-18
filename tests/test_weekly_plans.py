"""Weekly plan API: typed week goal; Done computed from the days and interview records."""

from datetime import datetime

from beanie import PydanticObjectId

from app.models import Interview, WeeklyPlan

WEEK = "2026-09-28"


def _week_body(**kw):
    body = {
        "goal": {"bidsSelf": 750, "bidsBidder": 500, "interviewsSelf": 10, "interviewsCaller": 5},
        "weeklyGoals": [{"text": "Profile update"}, {"text": "Email outreach", "kind": "count", "target": 250}],
        "routines": [{"text": "Seek caller"}, {"text": "Bidder review"}],
        "recapNotes": "",
    }
    body.update(kw)
    return body


def _follow_up(self_=0, bidder=0, results=None):
    return {"done": {"bidsSelf": self_, "bidsBidder": bidder}, "results": results or {}}


async def test_requires_auth(client):
    assert (await client.get(f"/weekly-plans/{WEEK}")).status_code == 401


async def test_put_and_get_week(client, staff_user, auth_header):
    h = auth_header(staff_user)
    r = await client.put(f"/weekly-plans/{WEEK}", headers=h, json=_week_body())
    assert r.status_code == 200, r.text
    week = r.json()
    assert week["weekStart"] == WEEK and week["exists"] is True
    assert week["goal"] == {"bidsSelf": 750, "bidsSelfUs": 0, "bidsSelfEu": 0, "bidsSelfLatam": 0, "bidsBidder": 500, "interviewsSelf": 10, "interviewsCaller": 5}
    assert [(i["text"], i["kind"], i["target"]) for i in week["weeklyGoals"]] == [
        ("Profile update", "check", None), ("Email outreach", "count", 250)]
    assert [i["text"] for i in week["routines"]] == ["Seek caller", "Bidder review"]
    assert all(i["id"] for i in week["routines"] + week["weeklyGoals"]) and week["draft"] is False
    assert "planNotes" not in week

    stored = await WeeklyPlan.find_one({"userId": staff_user.id})
    assert stored.weekStart == datetime(2026, 9, 28) and stored.startDate == datetime(2026, 9, 28)
    assert stored.endDate == datetime(2026, 10, 4, 23, 59, 59, 999000)
    assert (stored.year, stored.weekNumber) == (2026, 40)

    await client.put(f"/weekly-plans/{WEEK}", headers=h, json=_week_body(recapNotes="Done"))
    assert await WeeklyPlan.find({"userId": staff_user.id}).count() == 1
    assert (await client.get(f"/weekly-plans/{WEEK}", headers=h)).json()["recapNotes"] == "Done"


async def test_empty_week_template(client, staff_user, auth_header):
    week = (await client.get(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user))).json()
    assert week["exists"] is False and week["draft"] is False and week["goal"]["bidsSelf"] == 0
    assert [d["date"] for d in week["days"]] == [
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03",
    ]


async def test_week_done_from_logged_days_and_interviews(client, staff_user, auth_header):
    h = auth_header(staff_user)
    await client.put("/daily-plans/2026-09-28", headers=h, json=_follow_up(4, 1))
    await client.put("/daily-plans/2026-09-30", headers=h, json=_follow_up(6, 2))
    await client.put("/daily-plans/2026-10-04", headers=h, json=_follow_up(1, 0))  # Sunday
    seek, review = (await client.put(f"/weekly-plans/{WEEK}", headers=h, json=_week_body())).json()["routines"]
    await client.put("/daily-plans/2026-10-01", headers=h,
                     json=_follow_up(0, 0, {seek["id"]: {"done": True}, review["id"]: {"done": False}}))
    account_id = PydanticObjectId()
    await Interview(accountId=account_id, createdBy=staff_user.id, scheduledAt=datetime(2026, 9, 29, 15),
                    stage="intro", status="completed").insert()
    await Interview(accountId=account_id, createdBy=staff_user.id, scheduledAt=datetime(2026, 10, 1, 15),
                    stage="final", status="scheduled",
                    caller={"enabled": True, "startsAt": datetime(2026, 10, 1, 15)}).insert()

    week = (await client.get(f"/weekly-plans/{WEEK}", headers=h)).json()
    assert week["done"] == {"bidsSelf": 11, "bidsSelfUs": 0, "bidsSelfEu": 0, "bidsSelfLatam": 0, "bidsBidder": 3, "interviewsSelf": 1, "interviewsCaller": 1}
    assert week["stages"] == {"self": {"intro": 1}, "caller": {"final": 1}}
    days = {d["date"]: d for d in week["days"]}
    assert "2026-10-04" in days
    assert days["2026-09-28"]["logged"] is True and days["2026-09-29"]["logged"] is False
    assert days["2026-09-30"]["done"]["bidsSelf"] == 6
    # Every day's goal is the week's split over Mon-Fri.
    assert days["2026-09-29"]["goal"]["bidsSelf"] == 150 and days["2026-10-01"]["goal"]["bidsBidder"] == 100
    assert (days["2026-10-01"]["itemsDone"], days["2026-10-01"]["itemsTotal"]) == (1, 2)
    assert (days["2026-09-29"]["itemsDone"], days["2026-09-29"]["itemsTotal"]) == (0, 0)


async def test_week_without_plan_is_last_weeks_draft(client, staff_user, auth_header):
    h = auth_header(staff_user)
    last = (await client.put("/weekly-plans/2026-09-21", headers=h, json=_week_body())).json()
    week = (await client.get(f"/weekly-plans/{WEEK}", headers=h)).json()
    assert week["draft"] is True and week["exists"] is False and week["goal"]["bidsSelf"] == 750
    assert [r["id"] for r in week["routines"]] == [r["id"] for r in last["routines"]] and week["weeklyGoals"] == []
    assert await WeeklyPlan.find({"userId": staff_user.id}).count() == 1


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
    r = await client.get(other, headers=auth_header(staff_user_2))  # staff read other people's plans too
    assert r.status_code == 200 and r.json()["goal"]["bidsSelf"] == 750
    r = await client.get(other, headers=auth_header(admin_user))
    assert r.status_code == 200 and r.json()["goal"]["bidsSelf"] == 750


async def test_week_validation_422(client, staff_user, auth_header):
    h = auth_header(staff_user)
    assert (await client.get("/weekly-plans/2026-09-29", headers=h)).status_code == 422
    assert (await client.put("/weekly-plans/2026-09-29", headers=h, json=_week_body())).status_code == 422
    assert (await client.put(f"/weekly-plans/{WEEK}", headers=h,
                             json=_week_body(recapNotes="x" * 4001))).status_code == 422
    assert (await client.put(f"/weekly-plans/{WEEK}", headers=h,
                             json=_week_body(goal={"interviewsCaller": -1}))).status_code == 422


async def test_legacy_weekly_doc_readable_by_weekStart(client, staff_user, auth_header):
    await WeeklyPlan.get_motor_collection().insert_one({
        "userId": staff_user.id, "weekNumber": 40, "year": 2026,
        "startDate": datetime(2026, 9, 28, 4, 0), "endDate": datetime(2026, 10, 5, 3, 59),
        "metrics": [{"key": "bids", "label": "Job applies", "target": 40, "actual": 12},
                    {"key": "interviews", "label": "Interviews", "target": 5, "actual": 1}],
        "status": "planned", "content": "Old plan", "result": "Old recap",
        "createdAt": datetime(2026, 9, 28), "updatedAt": datetime(2026, 9, 28),
    })
    week = (await client.get(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user))).json()
    assert week["exists"] is True
    assert week["goal"]["bidsSelf"] == 40 and week["goal"]["interviewsSelf"] == 5
    assert week["recapNotes"] == "Old plan\n\nOld recap"
    await client.put(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user), json=_week_body())
    assert await WeeklyPlan.find({"userId": staff_user.id}).count() == 1


async def test_staff_reads_another_users_weekly_plan(client, staff_user, staff_user_2, auth_header):
    await client.put(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user), json=_week_body())
    r = await client.get(f"/weekly-plans/{WEEK}?userId={staff_user.id}", headers=auth_header(staff_user_2))
    assert r.status_code == 200 and r.json()["userId"] == str(staff_user.id) and r.json()["goal"]["bidsSelf"] == 750


async def test_staff_cannot_write_another_users_weekly_plan(client, staff_user, staff_user_2, auth_header):
    """No write route takes a user id: a userId in the query or body is ignored and the caller's own plan is written."""
    await client.put(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user), json=_week_body())
    mine = auth_header(staff_user_2)
    r = await client.put(f"/weekly-plans/{WEEK}?userId={staff_user.id}", headers=mine,
                         json={**_week_body(goal={"bidsSelf": 1}), "userId": str(staff_user.id)})
    assert r.status_code == 200 and r.json()["userId"] == str(staff_user_2.id)
    assert (await WeeklyPlan.find_one({"userId": staff_user.id})).goal.bidsSelf == 750  # untouched
    assert (await WeeklyPlan.find_one({"userId": staff_user_2.id})).goal.bidsSelf == 1
    assert (await client.delete(f"/weekly-plans/{WEEK}?userId={staff_user.id}", headers=mine)).status_code == 200
    assert await WeeklyPlan.find_one({"userId": staff_user_2.id}) is None
    assert await WeeklyPlan.find_one({"userId": staff_user.id}) is not None
    assert (await client.delete(f"/weekly-plans/{WEEK}?userId={staff_user.id}", headers=mine)).status_code == 404
    assert await WeeklyPlan.find_one({"userId": staff_user.id}) is not None
