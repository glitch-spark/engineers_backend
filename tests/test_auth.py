from app.models import UserExtra


async def test_register_first_user_becomes_admin(client):
    r = await client.post(
        "/auth/register",
        json={"username": "alice", "email": "alice@example.com", "password": "secret123"},
    )
    assert r.status_code == 201, r.text
    user = await UserExtra.find_one(UserExtra.email == "alice@example.com")
    assert user is not None
    assert user.role == "admin"


async def test_register_second_user_becomes_staff(client):
    await client.post(
        "/auth/register",
        json={"username": "alice", "email": "a@x.com", "password": "p"},
    )
    r = await client.post(
        "/auth/register",
        json={"username": "bob", "email": "b@x.com", "password": "p"},
    )
    assert r.status_code == 201
    user = await UserExtra.find_one(UserExtra.email == "b@x.com")
    assert user.role == "staff"


async def test_register_duplicate_email_returns_409(client):
    await client.post(
        "/auth/register",
        json={"username": "a", "email": "dup@x.com", "password": "p"},
    )
    r = await client.post(
        "/auth/register",
        json={"username": "a2", "email": "DUP@x.com", "password": "p"},
    )
    assert r.status_code == 409


async def test_register_missing_fields_returns_400(client):
    r = await client.post(
        "/auth/register",
        json={"username": "", "email": "", "password": ""},
    )
    assert r.status_code == 400


async def test_login_returns_token_and_user(client):
    await client.post(
        "/auth/register",
        json={"username": "alice", "email": "a@x.com", "password": "secret"},
    )
    r = await client.post(
        "/auth/login",
        json={"username": "alice", "password": "secret"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert "access_token" in body and body["access_token"]
    assert body["user"]["email"] == "a@x.com"
    assert body["user"]["role"] == "admin"  # first user


async def test_login_records_last_login_and_returns_account_dates(client):
    await client.post(
        "/auth/register",
        json={"username": "alice", "email": "a@x.com", "password": "secret"},
    )
    user = await UserExtra.find_one(UserExtra.email == "a@x.com")
    assert user.lastLoginAt is None

    r = await client.post("/auth/login", json={"username": "alice", "password": "secret"})
    assert r.status_code == 200, r.text
    out = r.json()["user"]
    assert out["createdAt"] and out["lastLoginAt"]
    # Serialised as UTC so browsers don't read it as local time.
    assert out["lastLoginAt"].endswith(("+00:00", "Z"))

    user = await UserExtra.find_one(UserExtra.email == "a@x.com")
    assert user.lastLoginAt is not None


async def test_login_works_with_email_or_name(client):
    await client.post(
        "/auth/register",
        json={"username": "alice", "email": "a@x.com", "password": "secret"},
    )
    by_email = await client.post("/auth/login", json={"username": "a@x.com", "password": "secret"})
    by_name = await client.post("/auth/login", json={"username": "alice", "password": "secret"})
    assert by_email.status_code == 200
    assert by_name.status_code == 200


async def test_login_wrong_password_returns_401(client):
    await client.post(
        "/auth/register",
        json={"username": "alice", "email": "a@x.com", "password": "secret"},
    )
    r = await client.post("/auth/login", json={"username": "alice", "password": "wrong"})
    assert r.status_code == 401


async def test_login_unknown_user_returns_401(client):
    r = await client.post("/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401


async def test_me_without_token_returns_401(client):
    r = await client.get("/auth/me")
    assert r.status_code == 401


async def test_me_with_token_returns_user(client, staff_user, auth_header):
    r = await client.get("/auth/me", headers=auth_header(staff_user))
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == staff_user.email
    assert body["role"] == "staff"
    assert body["id"] == str(staff_user.id)
