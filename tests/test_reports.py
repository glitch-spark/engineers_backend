"""Team report for a week: open to every active signed-in user."""

WEEK = "2026-09-28"


async def test_team_readable_by_staff(client, staff_user, staff_user_2, admin_user, auth_header):
    r = await client.get(f"/reports/team?weekStart={WEEK}", headers=auth_header(staff_user))
    assert r.status_code == 200
    assert [row["name"] for row in r.json()["users"]] == ["Staff One", "Staff Two"]  # same as the admin sees
    assert r.json() == (await client.get(f"/reports/team?weekStart={WEEK}", headers=auth_header(admin_user))).json()


async def test_team_needs_sign_in_and_active_account(client, staff_user, auth_header):
    assert (await client.get(f"/reports/team?weekStart={WEEK}")).status_code == 401
    staff_user.isActive = False
    await staff_user.save()
    assert (await client.get(f"/reports/team?weekStart={WEEK}", headers=auth_header(staff_user))).status_code == 403


async def test_team_requires_monday(client, admin_user, auth_header):
    r = await client.get("/reports/team?weekStart=2026-09-29", headers=auth_header(admin_user))
    assert r.status_code == 422


async def test_team_rows_include_users_without_plans(client, staff_user, staff_user_2, admin_user, auth_header):
    await client.put("/daily-plans/2026-09-30", headers=auth_header(staff_user),
                     json={"goal": {}, "done": {"bidsSelf": 5, "bidsBidder": 1}})
    await client.put(f"/weekly-plans/{WEEK}", headers=auth_header(staff_user),
                     json={"goal": {"bidsSelf": 30},
                           "goalItems": [{"text": "Profile update", "done": True}, {"text": "LinkedIn"}]})

    r = await client.get(f"/reports/team?weekStart={WEEK}", headers=auth_header(admin_user))
    assert r.status_code == 200
    body = r.json()
    assert body["weekStart"] == WEEK
    rows = {row["name"]: row for row in body["users"]}
    assert set(rows) == {"Staff One", "Staff Two"}  # admins are not listed
    assert [row["name"] for row in body["users"]] == ["Staff One", "Staff Two"]

    one = rows["Staff One"]
    assert one["userId"] == str(staff_user.id) and one["email"] == "staff@example.com"
    assert one["goal"]["bidsSelf"] == 30 and one["hasWeeklyPlan"] is True
    assert one["done"]["bidsSelf"] == 5 and one["done"]["bidsBidder"] == 1
    assert one["lastLoggedDate"] == "2026-09-30"
    assert (one["itemsDone"], one["itemsTotal"]) == (1, 2)

    two = rows["Staff Two"]
    assert two["hasWeeklyPlan"] is False and two["lastLoggedDate"] is None
    assert two["goal"]["bidsSelf"] == 0 and two["done"]["bidsSelf"] == 0
    assert (two["itemsDone"], two["itemsTotal"]) == (0, 0)


async def test_team_done_matches_week_endpoint(client, staff_user, admin_user, auth_header):
    h = auth_header(staff_user)
    await client.put("/daily-plans/2026-09-28", headers=h, json={"goal": {}, "done": {"bidsSelf": 4, "bidsBidder": 2}})
    await client.put(f"/weekly-plans/{WEEK}", headers=h, json={"goal": {"bidsSelf": 20}})

    team = (await client.get(f"/reports/team?weekStart={WEEK}", headers=auth_header(admin_user))).json()
    week = (await client.get(f"/weekly-plans/{WEEK}?userId={staff_user.id}", headers=auth_header(admin_user))).json()
    row = next(r for r in team["users"] if r["userId"] == str(staff_user.id))
    assert row["done"] == week["done"]
    assert row["goal"] == week["goal"]


async def test_team_last_logged_looks_before_this_week(client, staff_user, admin_user, auth_header):
    # Logged last Saturday only: on Monday the team view must still show that date.
    await client.put("/daily-plans/2026-09-26", headers=auth_header(staff_user),
                     json={"goal": {}, "done": {"bidsSelf": 2}})
    # Logs after the viewed week are ignored.
    await client.put("/daily-plans/2026-10-06", headers=auth_header(staff_user),
                     json={"goal": {}, "done": {"bidsSelf": 1}})

    team = (await client.get(f"/reports/team?weekStart={WEEK}", headers=auth_header(admin_user))).json()
    row = next(r for r in team["users"] if r["userId"] == str(staff_user.id))
    assert row["lastLoggedDate"] == "2026-09-26"
