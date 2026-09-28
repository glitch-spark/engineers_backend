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


async def test_old_plans_default_to_custom(client, staff_user, auth_header):
    r = await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-01-26", "today": "notes only"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["planType"] == "custom"
    assert body["bidsHandsOn"] == 0
    assert body["planBidsHandsOn"] == 0
    assert body["todayItems"] == []
    assert body["tomorrowItems"] == []


async def test_regular_plan_round_trip(client, staff_user, auth_header):
    r = await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={
            "date": "2026-09-25",
            "planType": "regular",
            "bidsHandsOn": 4,
            "bidsByBidder": 12,
            "interviewsDone": 1,
            "interviewsNew": 2,
            "todayItems": ["  Create Linkedin Profile ", "", "Update resume"],
            "planBidsHandsOn": 5,
            "planBidsByBidder": 15,
            "planInterviewsScheduled": 3,
            "tomorrowItems": ["Prep for Acme"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["planType"] == "regular"
    assert body["bidsByBidder"] == 12
    assert body["todayItems"] == ["Create Linkedin Profile", "Update resume"]
    assert body["planBidsByBidder"] == 15
    assert body["planInterviewsScheduled"] == 3
    assert body["tomorrowItems"] == ["Prep for Acme"]
    assert "customItems" not in body

    r = await client.put(
        f"/daily-plans/{body['_id']}",
        headers=auth_header(staff_user),
        json={"interviewsNew": 3, "planInterviewsScheduled": 4, "todayItems": []},
    )
    assert r.status_code == 200, r.text
    assert r.json()["interviewsNew"] == 3
    assert r.json()["planInterviewsScheduled"] == 4
    assert r.json()["bidsHandsOn"] == 4
    assert r.json()["todayItems"] == []
    assert r.json()["tomorrowItems"] == ["Prep for Acme"]


async def test_regular_plan_rejects_bad_values(client, staff_user, auth_header):
    base = {"date": "2026-09-25", "planType": "regular"}
    for extra in (
        {"bidsHandsOn": -1},
        {"planBidsByBidder": -1},
        {"planType": "weekly"},
        {"todayItems": ["x" * 201]},
        {"tomorrowItems": [f"item {n}" for n in range(21)]},
    ):
        r = await client.post("/daily-plans", headers=auth_header(staff_user), json={**base, **extra})
        assert r.status_code == 400, (extra, r.text)


async def test_counted_custom_items_read_back_as_today_items(client, staff_user, auth_header):
    """Plans saved while custom items had a number still load."""
    from datetime import datetime as dt

    from app.models.daily_plan import DailyPlan

    await DailyPlan.get_motor_collection().insert_one({
        "userId": staff_user.id,
        "date": dt(2026, 9, 26),
        "year": 2026,
        "weekNumber": 39,
        "planType": "regular",
        "bidsByBidder": 7,
        "customItems": [{"label": "Cold emails", "value": 5}, {"label": "Calls", "value": 2}],
        "createdAt": dt.utcnow(),
        "updatedAt": dt.utcnow(),
    })

    r = await client.get("/daily-plans", headers=auth_header(staff_user))
    assert r.status_code == 200, r.text
    plan = r.json()["plans"][0]
    assert plan["todayItems"] == ["Cold emails: 5", "Calls: 2"]
    assert plan["bidsByBidder"] == 7
    assert "customItems" not in plan


async def test_stats_sum_regular_plans_per_user_and_team(client, staff_user, staff_user_2, auth_header):
    await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-09-26", "planType": "regular", "bidsHandsOn": 4, "bidsByBidder": 10, "interviewsNew": 1},
    )
    await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-09-27", "planType": "custom", "bidsHandsOn": 99, "today": "not counted"},
    )
    await client.post(
        "/daily-plans",
        headers=auth_header(staff_user_2),
        json={"date": "2026-09-27", "planType": "regular", "bidsByBidder": 5, "interviewsDone": 2},
    )

    r = await client.get(
        f"/daily-plans/stats?bucket=day&to=2026-09-27&userId={staff_user.id}",
        headers=auth_header(staff_user_2),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window"] == {"from": "2026-09-14", "to": "2026-09-27"}
    assert len(body["series"]) == 14
    assert body["series"][-2]["bidsHandsOn"] == 4
    assert body["series"][-1]["bidsHandsOn"] == 0
    assert body["totals"]["bidsByBidder"] == 10
    assert body["totals"]["interviewsNew"] == 1

    r = await client.get("/daily-plans/stats?bucket=week&to=2026-09-27", headers=auth_header(staff_user))
    body = r.json()
    assert len(body["series"]) == 12
    assert body["totals"]["bidsByBidder"] == 15
    assert body["totals"]["interviewsDone"] == 2
    assert body["totals"]["planCount"] == 2


async def test_stats_put_planned_counts_on_the_next_day(client, staff_user, auth_header):
    # Dated the day before the 14-day window, so only its plan for 9/14 counts.
    await client.post(
        "/daily-plans",
        headers=auth_header(staff_user),
        json={"date": "2026-09-13", "planType": "regular", "bidsHandsOn": 9, "planBidsByBidder": 20},
    )
    r = await client.get(
        f"/daily-plans/stats?bucket=day&to=2026-09-27&userId={staff_user.id}",
        headers=auth_header(staff_user),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["series"][0]["bucketStart"] == "2026-09-14"
    assert body["series"][0]["planBidsByBidder"] == 20
    assert body["totals"]["bidsHandsOn"] == 0
    assert body["totals"]["planBidsByBidder"] == 20


async def test_stats_rejects_bad_bucket_and_date(client, staff_user, auth_header):
    r = await client.get("/daily-plans/stats?bucket=year", headers=auth_header(staff_user))
    assert r.status_code in (400, 422)
    r = await client.get("/daily-plans/stats?to=not-a-date", headers=auth_header(staff_user))
    assert r.status_code == 400
