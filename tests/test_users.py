async def test_users_list_no_token_returns_401(client):
    r = await client.get("/users")
    assert r.status_code == 401


async def test_users_list_staff_returns_403(client, staff_user, auth_header):
    r = await client.get("/users", headers=auth_header(staff_user))
    assert r.status_code == 403


async def test_users_list_admin_returns_paginated(client, admin_user, staff_user, auth_header):
    r = await client.get("/users", headers=auth_header(admin_user))
    assert r.status_code == 200
    body = r.json()
    assert "users" in body
    assert "pagination" in body
    p = body["pagination"]
    assert {"total", "page", "limit", "totalPages"}.issubset(p.keys())
    assert p["total"] >= 2

    # No password leaks
    for u in body["users"]:
        assert "hashedPassword" not in u


async def test_users_create_staff_returns_403(client, staff_user, auth_header):
    r = await client.post(
        "/users",
        headers=auth_header(staff_user),
        json={"name": "x", "email": "x@x.com"},
    )
    assert r.status_code == 403


async def test_users_create_admin(client, admin_user, auth_header):
    r = await client.post(
        "/users",
        headers=auth_header(admin_user),
        json={"name": "New User", "email": "new@example.com", "role": "staff"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "new@example.com"
    assert body["role"] == "staff"
    assert "hashedPassword" not in body


async def test_users_update_admin(client, admin_user, staff_user, auth_header):
    r = await client.put(
        f"/users/{staff_user.id}",
        headers=auth_header(admin_user),
        json={"name": "Updated Name", "role": "interview_manager"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Updated Name"
    assert body["role"] == "interview_manager"


async def test_users_delete_admin(client, admin_user, staff_user, auth_header):
    r = await client.delete(f"/users/{staff_user.id}", headers=auth_header(admin_user))
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_users_search_filter(client, admin_user, staff_user, auth_header):
    # staff_user has email "staff@example.com"; search by partial email
    r = await client.get("/users?search=staff", headers=auth_header(admin_user))
    assert r.status_code == 200
    emails = [u["email"] for u in r.json()["users"]]
    assert "staff@example.com" in emails
