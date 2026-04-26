async def test_get_profile_without_token_returns_401(client):
    r = await client.get("/profile")
    assert r.status_code == 401


async def test_get_profile_returns_user_shape(client, staff_user, auth_header):
    r = await client.get("/profile", headers=auth_header(staff_user))
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == staff_user.email
    assert body["user"]["username"] == staff_user.name


async def test_update_profile_changes_name_and_email(client, staff_user, auth_header):
    r = await client.put(
        "/profile",
        headers=auth_header(staff_user),
        json={"username": "Renamed", "email": "renamed@example.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["username"] == "Renamed"
    assert body["user"]["email"] == "renamed@example.com"


async def test_update_profile_missing_fields_returns_400(client, staff_user, auth_header):
    r = await client.put(
        "/profile",
        headers=auth_header(staff_user),
        json={"username": "", "email": ""},
    )
    assert r.status_code == 400


async def test_change_password_wrong_current_returns_400(client, staff_user, auth_header):
    r = await client.put(
        "/profile/password",
        headers=auth_header(staff_user),
        json={"currentPassword": "wrong", "newPassword": "newpass123"},
    )
    assert r.status_code == 400


async def test_change_password_too_short_returns_400(client, staff_user, auth_header, default_password):
    r = await client.put(
        "/profile/password",
        headers=auth_header(staff_user),
        json={"currentPassword": default_password, "newPassword": "x"},
    )
    assert r.status_code == 400


async def test_change_password_then_login_with_new(client, staff_user, auth_header, default_password):
    r = await client.put(
        "/profile/password",
        headers=auth_header(staff_user),
        json={"currentPassword": default_password, "newPassword": "newpassword"},
    )
    assert r.status_code == 200

    r2 = await client.post(
        "/auth/login",
        json={"username": staff_user.email, "password": "newpassword"},
    )
    assert r2.status_code == 200, r2.text
