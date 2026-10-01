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


# --- Resumes -------------------------------------------------------------

async def test_account_with_resumes_round_trip(client, staff_user, auth_header):
    """Create with 2 resumes, server assigns ids, list returns them, edit can drop one and add one."""
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={
            "name": "Acme",
            "email": "ops@acme.com",
            "resumes": [
                {"filename": "alice.md", "markdown": "# Alice\n\nSenior dev"},
                {"filename": "bob.txt", "markdown": "Bob\n\nJunior dev"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    acc = r.json()
    assert len(acc["resumes"]) == 2
    server_ids = [res["id"] for res in acc["resumes"]]
    assert all(server_ids), "server should assign ids to new resumes"
    keep_id = server_ids[0]

    # Edit: drop the second resume, keep the first (by id), add a third (no id).
    r = await client.put(
        f"/accounts/{acc['_id']}",
        headers=auth_header(staff_user),
        json={
            "resumes": [
                {"id": keep_id, "filename": "alice.md", "markdown": "# Alice v2"},
                {"filename": "carol.pdf.md", "markdown": "# Carol\n\nFresh grad"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert len(updated["resumes"]) == 2
    # The kept resume preserves its id; the new one gets a fresh id.
    ids = [res["id"] for res in updated["resumes"]]
    assert keep_id in ids
    assert any(rid != keep_id for rid in ids)
    # The kept resume's markdown was updated.
    kept = next(r for r in updated["resumes"] if r["id"] == keep_id)
    assert kept["markdown"] == "# Alice v2"


async def test_resume_count_cap_returns_400(client, staff_user, auth_header):
    too_many = [{"filename": f"f{i}.md", "markdown": "x"} for i in range(21)]
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={"name": "Big", "email": "b@x.com", "resumes": too_many},
    )
    assert r.status_code == 400


async def test_resume_oversized_markdown_returns_400(client, staff_user, auth_header):
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={
            "name": "Big",
            "email": "b@x.com",
            "resumes": [{"filename": "huge.md", "markdown": "x" * 200_001}],
        },
    )
    assert r.status_code == 400


async def test_resume_empty_filename_returns_400(client, staff_user, auth_header):
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={
            "name": "Bad",
            "email": "b@x.com",
            "resumes": [{"filename": "   ", "markdown": "x"}],
        },
    )
    assert r.status_code == 400


async def test_account_without_resumes_returns_empty_list(client, staff_user, auth_header):
    """Backwards-compat: omitting resumes defaults to empty list."""
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={"name": "Plain", "email": "p@x.com"},
    )
    assert r.status_code == 201
    assert r.json()["resumes"] == []


async def test_list_endpoint_strips_resume_markdown(client, staff_user, auth_header):
    """List response must not include full markdown — only id/filename/uploadedAt/sizeBytes."""
    big = "x" * 10_000
    await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={
            "name": "Acme",
            "email": "ops@acme.com",
            "resumes": [{"filename": "alice.md", "markdown": big}],
        },
    )
    r = await client.get("/accounts", headers=auth_header(staff_user))
    assert r.status_code == 200
    resumes = r.json()["accounts"][0]["resumes"]
    assert len(resumes) == 1
    assert "markdown" not in resumes[0]
    assert resumes[0]["filename"] == "alice.md"
    assert resumes[0]["sizeBytes"] == 10_000


async def test_get_account_by_id_returns_full_markdown(client, staff_user, auth_header):
    big = "y" * 5_000
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={
            "name": "Acme",
            "email": "ops@acme.com",
            "resumes": [{"filename": "bob.md", "markdown": big}],
        },
    )
    acc_id = r.json()["_id"]
    r = await client.get(f"/accounts/{acc_id}", headers=auth_header(staff_user))
    assert r.status_code == 200
    assert r.json()["resumes"][0]["markdown"] == big


async def test_get_account_by_id_other_user_returns_404(client, staff_user, staff_user_2, auth_header):
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={"name": "Mine", "email": "m@x.com"},
    )
    acc_id = r.json()["_id"]
    r = await client.get(f"/accounts/{acc_id}", headers=auth_header(staff_user_2))
    assert r.status_code == 404


async def test_get_account_by_id_admin_can_read_others(client, staff_user, admin_user, auth_header):
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={"name": "Mine", "email": "m@x.com"},
    )
    acc_id = r.json()["_id"]
    r = await client.get(f"/accounts/{acc_id}", headers=auth_header(admin_user))
    assert r.status_code == 200


async def test_resume_filename_strips_path_separators(client, staff_user, auth_header):
    """Path traversal attempts in filename are sanitized, not stored verbatim."""
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={
            "name": "Acme",
            "email": "ops@acme.com",
            "resumes": [{"filename": "../../etc/passwd", "markdown": "x"}],
        },
    )
    assert r.status_code == 201
    stored = r.json()["resumes"][0]["filename"]
    assert "/" not in stored
    assert "\\" not in stored
    # Underscore-substituted: "..______etc__passwd" or similar
    assert "etc" in stored and "passwd" in stored


async def test_resume_filename_strips_control_chars(client, staff_user, auth_header):
    r = await client.post(
        "/accounts",
        headers=auth_header(staff_user),
        json={
            "name": "Acme",
            "email": "ops@acme.com",
            "resumes": [{"filename": "ali\x00ce\x07.md", "markdown": "x"}],
        },
    )
    assert r.status_code == 201
    stored = r.json()["resumes"][0]["filename"]
    assert "\x00" not in stored and "\x07" not in stored


async def test_archive_hides_profile_from_default_list(client, staff_user, auth_header):
    h = auth_header(staff_user)
    r = await client.post("/accounts", headers=h, json={"name": "Old profile"})
    acc_id = r.json()["_id"]
    r = await client.put(f"/accounts/{acc_id}", headers=h, json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True and r.json()["archivedAt"]
    assert [a["_id"] for a in (await client.get("/accounts", headers=h)).json()["accounts"]] == []
    archived = (await client.get("/accounts?status=archived", headers=h)).json()["accounts"]
    assert [a["_id"] for a in archived] == [acc_id]
    assert len((await client.get("/accounts?status=all", headers=h)).json()["accounts"]) == 1
    lookup = (await client.get("/accounts/lookup", headers=h)).json()["accounts"]
    assert lookup[0]["archived"] is True
    r = await client.put(f"/accounts/{acc_id}", headers=h, json={"archived": False})
    assert r.json()["archived"] is False and r.json()["archivedAt"] is None
