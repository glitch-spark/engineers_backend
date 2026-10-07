from datetime import datetime

from beanie import PydanticObjectId

from app.models import Transaction


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


async def test_staff_sees_all_transactions(client, staff_user, staff_user_2, auth_header):
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-01-01", "amount": 1, "payMethod": "coin"},
    )
    r = await client.get("/transactions", headers=auth_header(staff_user_2))
    assert r.json()["pagination"]["total"] == 1


async def test_staff_can_filter_by_owner(client, staff_user, staff_user_2, auth_header):
    for owner in (staff_user, staff_user_2):
        await client.post(
            "/transactions",
            headers=auth_header(owner),
            json={"date": "2026-01-01", "amount": 1, "payMethod": "coin"},
        )
    r = await client.get(
        f"/transactions?userId={staff_user.id}", headers=auth_header(staff_user_2),
    )
    body = r.json()
    assert body["pagination"]["total"] == 1
    assert body["transactions"][0]["ownerEmail"] == staff_user.email


async def test_filter_by_payer_includes_legacy_rows(client, staff_user, admin_user, auth_header):
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-01-01", "amount": -5, "payMethod": "coin", "payerId": str(admin_user.id)},
    )
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-01-02", "amount": -7, "payMethod": "coin"},
    )
    # Pre-payer row: no payerId stored, so the owner counts as the payer.
    legacy = Transaction(userId=staff_user.id, date=datetime(2026, 1, 3), amount=-9, payMethod="coin")
    await legacy.insert()

    r = await client.get(
        f"/transactions?payerId={staff_user.id}", headers=auth_header(staff_user),
    )
    body = r.json()
    assert body["pagination"]["total"] == 2
    assert sorted(t["amount"] for t in body["transactions"]) == [-9, -7]

    r = await client.get(
        f"/transactions?payerId={admin_user.id}", headers=auth_header(staff_user),
    )
    assert r.json()["pagination"]["total"] == 1


async def test_totals_cover_all_pages_and_respect_filters(client, staff_user, auth_header):
    for date, amount, method in (
        ("2026-02-02", 20, "coin"),
        ("2026-02-03", -5, "coin"),
        ("2026-02-04", -10, "card"),
        ("2026-03-01", -100, "coin"),
    ):
        body = {"date": date, "amount": amount, "payMethod": method}
        if method == "card":
            body["cardLast4"] = "1234"
        await client.post("/transactions", headers=auth_header(staff_user), json=body)

    r = await client.get(
        "/transactions?from=2026-02-01&to=2026-02-28&limit=1", headers=auth_header(staff_user),
    )
    body = r.json()
    assert len(body["transactions"]) == 1
    assert body["totals"] == {"income": 20, "outcome": 15, "net": 5, "count": 3}

    r = await client.get(
        "/transactions?from=2026-02-01&to=2026-02-28&payMethod=coin", headers=auth_header(staff_user),
    )
    assert r.json()["totals"] == {"income": 20, "outcome": 5, "net": 15, "count": 2}

    r = await client.get("/transactions?from=2030-01-01", headers=auth_header(staff_user))
    assert r.json()["totals"] == {"income": 0, "outcome": 0, "net": 0, "count": 0}


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


async def test_create_defaults_payer_to_owner(client, staff_user, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-03-01", "amount": -10, "payMethod": "coin"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["payerId"] == str(staff_user.id)

    listed = await client.get("/transactions", headers=auth_header(staff_user))
    assert listed.status_code == 200
    row = listed.json()["transactions"][0]
    assert row["payerName"] == staff_user.name
    assert row["payerEmail"] == staff_user.email


async def test_create_with_admin_payer(client, staff_user, admin_user, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={
            "date": "2026-03-02",
            "amount": -25,
            "payMethod": "coin",
            "payerId": str(admin_user.id),
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["payerId"] == str(admin_user.id)

    listed = await client.get("/transactions", headers=auth_header(staff_user))
    row = listed.json()["transactions"][0]
    assert row["ownerEmail"] == staff_user.email
    assert row["payerEmail"] == admin_user.email
    assert row["payerName"] == admin_user.name


async def test_create_unknown_payer_returns_400(client, staff_user, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={
            "date": "2026-03-03",
            "amount": -5,
            "payMethod": "coin",
            "payerId": "000000000000000000000000",
        },
    )
    assert r.status_code == 400


async def test_payer_totals_split_and_total_outcome(client, staff_user, admin_user, auth_header):
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={
            "date": "2026-03-04",
            "amount": -40,
            "payMethod": "coin",
            "payerId": str(admin_user.id),
        },
    )
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={
            "date": "2026-03-05",
            "amount": -10,
            "payMethod": "coin",
            "payerId": str(staff_user.id),
        },
    )
    await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-03-06", "amount": 15, "payMethod": "coin"},
    )

    r = await client.get("/transactions", headers=auth_header(admin_user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "payerTotals" in body
    by_payer = {p["payerId"]: p for p in body["payerTotals"]}
    assert by_payer[str(admin_user.id)]["outcome"] == 40
    assert by_payer[str(staff_user.id)]["outcome"] == 10
    assert by_payer[str(staff_user.id)]["income"] == 15
    assert by_payer[str(staff_user.id)]["net"] == 5
    assert body["totalOutcome"] == 50


async def test_update_payer(client, staff_user, admin_user, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-03-07", "amount": -8, "payMethod": "coin"},
    )
    tx_id = r.json()["_id"]
    r = await client.put(
        f"/transactions/{tx_id}",
        headers=auth_header(staff_user),
        json={"payerId": str(admin_user.id)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["payerId"] == str(admin_user.id)


async def test_approve_with_populated_payer_object(client, staff_user, admin_user, auth_header):
    """SPA used to spread the listed row (payerId as a user dict) on approve."""
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={
            "date": "2026-03-08",
            "amount": -12,
            "payMethod": "coin",
            "payerId": str(admin_user.id),
        },
    )
    tx_id = r.json()["_id"]
    r = await client.put(
        f"/transactions/{tx_id}",
        headers=auth_header(admin_user),
        json={
            "status": "approved",
            "payerId": {
                "_id": str(admin_user.id),
                "name": admin_user.name,
                "email": admin_user.email,
            },
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["approvedBy"] == str(admin_user.id)


async def test_payer_staff_can_list_and_approve(client, staff_user, staff_user_2, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={
            "date": "2026-03-09",
            "amount": -30,
            "payMethod": "coin",
            "payerId": str(staff_user_2.id),
        },
    )
    tx_id = r.json()["_id"]

    listed = await client.get("/transactions", headers=auth_header(staff_user_2))
    assert listed.status_code == 200
    ids = [t["_id"] for t in listed.json()["transactions"]]
    assert tx_id in ids

    r = await client.put(
        f"/transactions/{tx_id}",
        headers=auth_header(staff_user_2),
        json={"status": "approved"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


async def test_non_payer_staff_cannot_approve(client, staff_user, staff_user_2, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-03-10", "amount": -4, "payMethod": "coin"},
    )
    tx_id = r.json()["_id"]
    r = await client.put(
        f"/transactions/{tx_id}",
        headers=auth_header(staff_user_2),
        json={"status": "approved"},
    )
    assert r.status_code == 403


async def test_self_payer_admin_can_request_and_approve(client, admin_user, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(admin_user),
        json={
            "date": "2026-03-11",
            "amount": -9,
            "payMethod": "coin",
            "payerId": str(admin_user.id),
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["payerId"] == str(admin_user.id)
    assert r.json()["userId"] == str(admin_user.id)
    tx_id = r.json()["_id"]
    r = await client.put(
        f"/transactions/{tx_id}",
        headers=auth_header(admin_user),
        json={"status": "approved"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"



async def test_self_paid_staff_cannot_approve_or_reject(client, staff_user, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-03-12", "amount": -9, "payMethod": "coin", "payerId": str(staff_user.id)},
    )
    tx_id = r.json()["_id"]
    for decision in ("approved", "rejected"):
        r = await client.put(f"/transactions/{tx_id}", headers=auth_header(staff_user), json={"status": decision})
        assert r.status_code == 403
    tx = await Transaction.get(PydanticObjectId(tx_id))
    assert tx.status == "pending"


async def test_owner_switching_payer_to_self_cannot_approve(client, staff_user, staff_user_2, auth_header):
    r = await client.post(
        "/transactions",
        headers=auth_header(staff_user),
        json={"date": "2026-03-13", "amount": -9, "payMethod": "coin", "payerId": str(staff_user_2.id)},
    )
    tx_id = r.json()["_id"]
    r = await client.put(f"/transactions/{tx_id}", headers=auth_header(staff_user), json={"payerId": str(staff_user.id)})
    assert r.status_code == 200
    r = await client.put(f"/transactions/{tx_id}", headers=auth_header(staff_user), json={"status": "approved"})
    assert r.status_code == 403
