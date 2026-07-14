async def test_weekly_plans_list_no_token_returns_401(client):
    r = await client.get("/weekly-plans")
    assert r.status_code == 401


async def test_weekly_plan_crud_round_trip(client, staff_user, auth_header):
    r = await client.post(
        "/weekly-plans",
        headers=auth_header(staff_user),
        json={
            "weekNumber": 5,
            "year": 2026,
            "startDate": "2026-01-26",
            "endDate": "2026-02-01",
            "content": "Plan for week 5",
            "result": "",
        },
    )
    assert r.status_code == 201, r.text
    plan_id = r.json()["_id"]

    r = await client.get(f"/weekly-plans/{plan_id}", headers=auth_header(staff_user))
    assert r.status_code == 200
    assert r.json()["weekNumber"] == 5

    r = await client.get("/weekly-plans", headers=auth_header(staff_user))
    assert r.json()["pagination"]["total"] == 1

    r = await client.put(
        f"/weekly-plans/{plan_id}",
        headers=auth_header(staff_user),
        json={"result": "Done"},
    )
    assert r.status_code == 200
    assert r.json()["result"] == "Done"

    r = await client.delete(f"/weekly-plans/{plan_id}", headers=auth_header(staff_user))
    assert r.status_code == 200


async def test_post_missing_required_returns_400(client, staff_user, auth_header):
    r = await client.post(
        "/weekly-plans",
        headers=auth_header(staff_user),
        json={"weekNumber": 1, "year": 2026},  # missing dates and content
    )
    assert r.status_code == 400


async def test_duplicate_weekly_plan_returns_400(client, staff_user, auth_header):
    body = {
        "weekNumber": 5, "year": 2026,
        "startDate": "2026-01-26", "endDate": "2026-02-01",
        "content": "First", "result": "",
    }
    r = await client.post("/weekly-plans", headers=auth_header(staff_user), json=body)
    assert r.status_code == 201

    r = await client.post(
        "/weekly-plans",
        headers=auth_header(staff_user),
        json={**body, "content": "Second"},
    )
    assert r.status_code == 400


async def test_staff_sees_all_plans(client, staff_user, staff_user_2, auth_header):
    await client.post(
        "/weekly-plans",
        headers=auth_header(staff_user),
        json={
            "weekNumber": 1, "year": 2026,
            "startDate": "2026-01-01", "endDate": "2026-01-07",
            "content": "x",
        },
    )
    r = await client.get("/weekly-plans", headers=auth_header(staff_user_2))
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1


async def test_admin_sees_all_plans(client, staff_user, admin_user, auth_header):
    await client.post(
        "/weekly-plans",
        headers=auth_header(staff_user),
        json={
            "weekNumber": 1, "year": 2026,
            "startDate": "2026-01-01", "endDate": "2026-01-07",
            "content": "x",
        },
    )
    r = await client.get("/weekly-plans", headers=auth_header(admin_user))
    assert r.json()["pagination"]["total"] == 1


async def test_other_staff_can_get_plan_but_not_edit(client, staff_user, staff_user_2, auth_header):
    r = await client.post(
        "/weekly-plans",
        headers=auth_header(staff_user),
        json={
            "weekNumber": 1, "year": 2026,
            "startDate": "2026-01-01", "endDate": "2026-01-07",
            "content": "x",
        },
    )
    plan_id = r.json()["_id"]
    r = await client.get(f"/weekly-plans/{plan_id}", headers=auth_header(staff_user_2))
    assert r.status_code == 200
    assert r.json()["content"] == "x"

    r = await client.put(
        f"/weekly-plans/{plan_id}",
        headers=auth_header(staff_user_2),
        json={"content": "hijacked"},
    )
    assert r.status_code == 403

    r = await client.delete(f"/weekly-plans/{plan_id}", headers=auth_header(staff_user_2))
    assert r.status_code == 403


async def test_staff_can_filter_by_user(client, staff_user, staff_user_2, auth_header):
    await client.post(
        "/weekly-plans",
        headers=auth_header(staff_user),
        json={
            "weekNumber": 1, "year": 2026,
            "startDate": "2026-01-01", "endDate": "2026-01-07",
            "content": "a",
        },
    )
    await client.post(
        "/weekly-plans",
        headers=auth_header(staff_user_2),
        json={
            "weekNumber": 1, "year": 2026,
            "startDate": "2026-01-01", "endDate": "2026-01-07",
            "content": "b",
        },
    )
    r = await client.get(
        f"/weekly-plans?userId={staff_user.id}",
        headers=auth_header(staff_user_2),
    )
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1
    assert r.json()["plans"][0]["content"] == "a"

    r = await client.get(
        "/weekly-plans/user-rollup?year=2026&weekNumber=1",
        headers=auth_header(staff_user_2),
    )
    assert r.status_code == 200
    assert len(r.json()["users"]) == 2
