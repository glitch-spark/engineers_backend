from datetime import datetime, timedelta


async def test_daily_plans_list_no_token_returns_401(client):
    r = await client.get("/daily-plans")
    assert r.status_code == 401


async def test_daily_plan_crud_round_trip(client, staff_user, auth_header):
    r = await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-01-26", "today": "Shipped the filter", "tomorrow": "Write tests"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    plan_id = body["_id"]
    assert body["today"] == "Shipped the filter"
    assert body["year"] == 2026
    assert body["weekNumber"] == 5

    r = await client.get("/daily-plans", headers=auth_header(staff_user))
    assert r.json()["pagination"]["total"] == 1

    r = await client.put(
        f"/daily-plans/{plan_id}",
        headers=auth_header(staff_user),
        json={"tomorrow": "Review the PR"},
    )
    assert r.status_code == 200
    assert r.json()["tomorrow"] == "Review the PR"
    assert r.json()["today"] == "Shipped the filter"

    r = await client.delete(f"/daily-plans/{plan_id}", headers=auth_header(staff_user))
    assert r.status_code == 200

    r = await client.get("/daily-plans", headers=auth_header(staff_user))
    assert r.json()["pagination"]["total"] == 0


async def test_post_missing_date_returns_400(client, staff_user, auth_header):
    r = await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"today": "no date"},
    )
    assert r.status_code == 400


async def test_duplicate_daily_plan_returns_400(client, staff_user, auth_header):
    body = {"date": "2026-01-26", "today": "First", "tomorrow": ""}
    r = await client.post("/daily-plans", headers=auth_header(staff_user), json=body)
    assert r.status_code == 201

    r = await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={**body, "today": "Second"},
    )
    assert r.status_code == 400


async def test_other_staff_can_list_but_not_edit(client, staff_user, staff_user_2, auth_header):
    r = await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-01-26", "today": "mine"},
    )
    plan_id = r.json()["_id"]

    r = await client.get("/daily-plans", headers=auth_header(staff_user_2))
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1
    assert r.json()["plans"][0]["today"] == "mine"

    r = await client.put(
        f"/daily-plans/{plan_id}",
        headers=auth_header(staff_user_2),
        json={"today": "hijacked"},
    )
    assert r.status_code == 403

    r = await client.delete(f"/daily-plans/{plan_id}", headers=auth_header(staff_user_2))
    assert r.status_code == 403


async def test_staff_can_filter_by_user_year_and_week(client, staff_user, staff_user_2, auth_header):
    await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-01-26", "today": "week five"},
    )
    await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-02-02", "today": "week six"},
    )
    await client.post(
        "/daily-plans",
        headers=auth_header(staff_user_2),
        json={"date": "2026-01-26", "today": "other user"},
    )

    r = await client.get(
        f"/daily-plans?year=2026&weekNumber=5&userId={staff_user.id}",
        headers=auth_header(staff_user_2),
    )
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1
    assert r.json()["plans"][0]["today"] == "week five"

    r = await client.get(
        "/daily-plans?year=2026&weekNumber=5",
        headers=auth_header(staff_user),
    )
    assert r.json()["pagination"]["total"] == 2


async def test_admin_lists_newest_created_but_cannot_write(client, staff_user, admin_user, auth_header):
    from app.models.daily_plan import DailyPlan

    first = await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-01-27", "today": "later day, created first"},
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-01-26", "today": "earlier day, created second"},
    )
    assert second.status_code == 201, second.text

    earlier = await DailyPlan.get(second.json()["_id"])
    earlier.createdAt = datetime.utcnow() + timedelta(hours=1)
    await earlier.save()

    staff_list = await client.get(
        "/daily-plans?year=2026&weekNumber=5",
        headers=auth_header(staff_user),
    )
    assert [p["date"][:10] for p in staff_list.json()["plans"]] == ["2026-01-27", "2026-01-26"]

    admin_list = await client.get(
        "/daily-plans?year=2026&weekNumber=5",
        headers=auth_header(admin_user),
    )
    assert admin_list.status_code == 200
    assert [p["today"] for p in admin_list.json()["plans"]] == [
        "earlier day, created second",
        "later day, created first",
    ]

    plan_id = first.json()["_id"]
    denied = await client.post(
        "/daily-plans",
        headers=auth_header(admin_user),
        json={"date": "2026-01-28", "today": "admin note"},
    )
    assert denied.status_code == 403

    denied = await client.put(
        f"/daily-plans/{plan_id}",
        headers=auth_header(admin_user),
        json={"today": "edited"},
    )
    assert denied.status_code == 403

    denied = await client.delete(f"/daily-plans/{plan_id}", headers=auth_header(admin_user))
    assert denied.status_code == 403
