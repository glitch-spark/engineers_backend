async def test_transactions_list_no_token_returns_401(client):
    r = await client.get("/transactions")
    assert r.status_code == 401


async def test_transaction_crud_round_trip(client, staff_user, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-01-15", "amount": 100.50, "description": "Lunch"},
    )
    assert r.status_code == 201, r.text
    tx_id = r.json()["_id"]
    assert r.json()["status"] == "pending"

    r = await client.get("/transactions", headers=auth_header(staff_user))
    assert r.status_code == 200
    body = r.json()
    assert body["pagination"]["total"] == 1
    assert body["transactions"][0]["ownerEmail"] == staff_user.email

    r = await client.put(
        f"/transactions/{tx_id}",
        headers=auth_header(staff_user),
        json={"description": "Updated"},
    )
    assert r.status_code == 200
    assert r.json()["description"] == "Updated"

    r = await client.delete(f"/transactions/{tx_id}", headers=auth_header(staff_user))
    assert r.status_code == 200


async def test_post_missing_required_returns_400(client, staff_user, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"description": "no date"},
    )
    # Either FastAPI's pydantic validation (400 via our remap) or our check
    assert r.status_code == 400


async def test_admin_status_change_sets_approved_fields(client, staff_user, admin_user, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-02-01", "amount": 50.0},
    )
    tx_id = r.json()["_id"]

    r = await client.put(
        f"/transactions/{tx_id}",
        headers=auth_header(admin_user),
        json={"status": "approved"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["approvedBy"] == str(admin_user.id)
    assert body["approvedAt"] is not None


async def test_staff_cannot_edit_others_transaction(client, staff_user, staff_user_2, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-01-01", "amount": 10},
    )
    tx_id = r.json()["_id"]

    r = await client.put(
        f"/transactions/{tx_id}",
        headers=auth_header(staff_user_2),
        json={"description": "Stolen"},
    )
    assert r.status_code == 403


async def test_staff_sees_own_transactions_only(client, staff_user, staff_user_2, auth_header):
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-01-01", "amount": 1},
    )
    r = await client.get("/transactions", headers=auth_header(staff_user_2))
    assert r.json()["pagination"]["total"] == 0


async def test_admin_sees_all_transactions(client, staff_user, admin_user, auth_header):
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-01-01", "amount": 1},
    )
    r = await client.get("/transactions", headers=auth_header(admin_user))
    assert r.json()["pagination"]["total"] == 1


async def test_summary_returns_expected_shape(client, admin_user, staff_user, auth_header):
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-01-15", "amount": 100},
    )
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-02-20", "amount": 50},
    )

    r = await client.get("/transactions/summary?year=2026", headers=auth_header(admin_user))
    assert r.status_code == 200, r.text
    body = r.json()

    assert "monthly" in body and isinstance(body["monthly"], list)
    assert "stats" in body
    assert "statusBreakdown" in body

    for key in ("totalAmount", "totalCount", "avgAmount", "minAmount", "maxAmount"):
        assert key in body["stats"]

    assert body["stats"]["totalCount"] == 2
    assert body["stats"]["totalAmount"] == 150

    assert "pending" in body["statusBreakdown"]
    assert body["statusBreakdown"]["pending"]["count"] == 2
    assert body["statusBreakdown"]["pending"]["total"] == 150

    periods = sorted(m["period"] for m in body["monthly"])
    assert periods == ["2026-01", "2026-02"]
    for m in body["monthly"]:
        assert m["year"] == 2026


async def test_summary_staff_sees_only_own(client, staff_user, staff_user_2, auth_header):
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-01-15", "amount": 100},
    )
    r = await client.get("/transactions/summary?year=2026", headers=auth_header(staff_user_2))
    assert r.status_code == 200
    assert r.json()["stats"]["totalCount"] == 0
