async def test_cardlinks_list_no_token_returns_401(client):
    r = await client.get("/cardlinks")
    assert r.status_code == 401


async def test_cardlink_crud_round_trip(client, staff_user, auth_header):
    r = await client.post(
        "/cardlinks",
        headers=auth_header(staff_user),
        json={
            "email": "c@x.com",
            "cardNumber": "4111111111111111",
            "site": "amazon.com",
            "from": "2026-01-01",
            "to": "2026-12-31",
        },
    )
    assert r.status_code == 201, r.text
    cl_id = r.json()["_id"]
    assert r.json()["status"] == "billing"
    # `from` is the wire field name (Python attr is `from_`); the alias must round-trip.
    assert r.json()["from"] is not None

    r = await client.get("/cardlinks", headers=auth_header(staff_user))
    assert r.json()["pagination"]["total"] == 1
    assert r.json()["cardlinks"][0]["ownerEmail"] == staff_user.email

    r = await client.put(
        f"/cardlinks/{cl_id}",
        headers=auth_header(staff_user),
        json={"site": "amazon.co.uk"},
    )
    assert r.status_code == 200
    assert r.json()["site"] == "amazon.co.uk"

    r = await client.delete(f"/cardlinks/{cl_id}", headers=auth_header(staff_user))
    assert r.status_code == 200


async def test_post_missing_required_returns_400(client, staff_user, auth_header):
    r = await client.post(
        "/cardlinks",
        headers=auth_header(staff_user),
        json={"email": "e@x.com"},  # missing cardNumber, from, to
    )
    assert r.status_code == 400


async def test_staff_sees_own_cardlinks_only(client, staff_user, staff_user_2, auth_header):
    await client.post(
        "/cardlinks",
        headers=auth_header(staff_user),
        json={
            "email": "x@x.com", "cardNumber": "4111", "site": "x",
            "from": "2026-01-01", "to": "2026-12-31",
        },
    )
    r = await client.get("/cardlinks", headers=auth_header(staff_user_2))
    assert r.json()["pagination"]["total"] == 0


async def test_admin_sees_all_cardlinks(client, staff_user, admin_user, auth_header):
    await client.post(
        "/cardlinks",
        headers=auth_header(staff_user),
        json={
            "email": "x@x.com", "cardNumber": "4111", "site": "x",
            "from": "2026-01-01", "to": "2026-12-31",
        },
    )
    r = await client.get("/cardlinks", headers=auth_header(admin_user))
    assert r.json()["pagination"]["total"] == 1


async def test_other_staff_cannot_edit_cardlink(client, staff_user, staff_user_2, auth_header):
    r = await client.post(
        "/cardlinks",
        headers=auth_header(staff_user),
        json={
            "email": "x@x.com", "cardNumber": "4111", "site": "x",
            "from": "2026-01-01", "to": "2026-12-31",
        },
    )
    cl_id = r.json()["_id"]
    r = await client.put(
        f"/cardlinks/{cl_id}",
        headers=auth_header(staff_user_2),
        json={"site": "hacked"},
    )
    assert r.status_code == 403
