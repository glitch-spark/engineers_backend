async def test_accounts_list_no_token_returns_401(client):
    r = await client.get("/accounts")
    assert r.status_code == 401


async def test_account_crud_round_trip(client, staff_user, auth_header):
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={"name": "Acme", "email": "ops@acme.com", "phone": "555", "address": "1 St"},
    )
    assert r.status_code == 201, r.text
    acc_id = r.json()["_id"]

    r = await client.get("/accounts", headers=auth_header(staff_user))
    assert r.status_code == 200
    body = r.json()
    assert len(body["accounts"]) == 1
    assert body["accounts"][0]["name"] == "Acme"
    assert body["accounts"][0]["ownerEmail"] == staff_user.email
    assert body["accounts"][0]["ownerName"] == staff_user.name

    r = await client.put(
        f"/accounts/{acc_id}",
        headers=auth_header(staff_user),
        json={"name": "Acme Inc"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Acme Inc"

    r = await client.delete(f"/accounts/{acc_id}", headers=auth_header(staff_user))
    assert r.status_code == 200

    r = await client.get("/accounts", headers=auth_header(staff_user))
    assert r.json()["pagination"]["total"] == 0


async def test_staff_sees_own_accounts_only(client, staff_user, staff_user_2, auth_header):
    await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={"name": "Owned by 1", "email": "1@x.com"},
    )
    r = await client.get("/accounts", headers=auth_header(staff_user_2))
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 0


async def test_admin_sees_all_accounts(client, staff_user, admin_user, auth_header):
    await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={"name": "Owned by staff", "email": "s@x.com"},
    )
    r = await client.get("/accounts", headers=auth_header(admin_user))
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1


async def test_admin_can_filter_by_userid(client, staff_user, staff_user_2, admin_user, auth_header):
    # staff_user creates one, staff_user_2 creates one
    await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={"name": "S1", "email": "s1@x.com"},
    )
    await client.post(
        "/accounts",
        headers=auth_header(staff_user_2),
        json={"name": "S2", "email": "s2@x.com"},
    )
    r = await client.get(f"/accounts?userId={staff_user.id}", headers=auth_header(admin_user))
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1
    assert r.json()["accounts"][0]["name"] == "S1"


async def test_other_staff_cannot_update_account(client, staff_user, staff_user_2, auth_header):
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={"name": "Mine", "email": "m@x.com"},
    )
    acc_id = r.json()["_id"]

    r = await client.put(
        f"/accounts/{acc_id}",
        headers=auth_header(staff_user_2),
        json={"name": "Hacked"},
    )
    # source returns 404 on ownership mismatch, not 403
    assert r.status_code == 404
