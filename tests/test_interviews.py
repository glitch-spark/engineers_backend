from datetime import datetime, timedelta

from app.models import Account
from app.models.account import Resume


async def _make_account(*, owner_id, name="Acme", email="acme@example.com", with_resume=True) -> Account:
    """Account with one default resume so interview tests have a valid resumeId to use."""
    resumes = [Resume(filename="default.md", markdown="# Default")] if with_resume else []
    acc = Account(name=name, email=email, createdBy=owner_id, resumes=resumes)
    await acc.insert()
    return acc


def _ends(scheduled_iso: str, *, minutes: int = 60) -> str:
    dt = datetime.fromisoformat(scheduled_iso.replace("Z", "+00:00"))
    return (dt + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _payload(*, account, scheduled_at, stage="intro", resume_id=None, **extras):
    """Build an interview payload. `account` must be an Account with at least one resume,
    OR `resume_id` must be passed explicitly (for negative tests)."""
    rid = resume_id if resume_id is not None else str(account.resumes[0].id)
    body = {
        "accountId": str(account.id),
        "resumeId": rid,
        "scheduledAt": scheduled_at,
        "endsAt": _ends(scheduled_at),
        "stage": stage,
    }
    body.update(extras)
    return body


async def test_interviews_list_no_token_returns_401(client):
    r = await client.get("/interviews")
    assert r.status_code == 401


async def test_interview_crud_round_trip(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)

    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(
            account=acc,
            scheduled_at="2026-04-25T14:30:00Z",
            stage="tech1",
            jobDescription="<p>Senior dev</p>",
            transcript="<p>Great chat</p>",
            note="<p>Strong</p>",
        ),
    )
    assert r.status_code == 201, r.text
    iv_id = r.json()["_id"]
    assert r.json()["endsAt"] is not None
    assert r.json()["resumeId"] == str(acc.resumes[0].id)

    r = await client.get("/interviews", headers=auth_header(staff_user))
    assert r.status_code == 200
    body = r.json()
    assert body["pagination"]["total"] == 1
    row = body["interviews"][0]
    assert row["ownerEmail"] == staff_user.email
    assert row["accountId"]["email"] == "acme@example.com"
    assert row["stage"] == "tech1"
    assert row["endsAt"] is not None
    assert row["resumeFilename"] == "default.md"

    r = await client.put(
        f"/interviews/{iv_id}",
        headers=auth_header(staff_user),
        json={"stage": "tech2"},
    )
    assert r.status_code == 200
    assert r.json()["stage"] == "tech2"

    r = await client.delete(f"/interviews/{iv_id}", headers=auth_header(staff_user))
    assert r.status_code == 200


async def test_create_invalid_stage_returns_400(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc, scheduled_at="2026-04-25T14:30:00Z", stage="bogus"),
    )
    assert r.status_code == 400


async def test_panel_stage_accepted(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc, scheduled_at="2026-04-25T14:30:00Z", stage="panel"),
    )
    assert r.status_code == 201
    assert r.json()["stage"] == "panel"


async def test_create_invalid_scheduledAt_returns_400(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json={
            "accountId": str(acc.id),
            "resumeId": str(acc.resumes[0].id),
            "scheduledAt": "not-a-date",
            "endsAt": "2026-04-25T15:30:00Z",
            "stage": "intro",
        },
    )
    assert r.status_code == 400


async def test_create_endsAt_before_scheduledAt_returns_400(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json={
            "accountId": str(acc.id),
            "resumeId": str(acc.resumes[0].id),
            "scheduledAt": "2026-04-25T14:30:00Z",
            "endsAt": "2026-04-25T14:00:00Z",
            "stage": "intro",
        },
    )
    assert r.status_code == 400


async def test_create_missing_endsAt_returns_400(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json={
            "accountId": str(acc.id),
            "resumeId": str(acc.resumes[0].id),
            "scheduledAt": "2026-04-25T14:30:00Z",
            "stage": "intro",
        },
    )
    assert r.status_code == 400


async def test_create_missing_resumeId_returns_400(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json={
            "accountId": str(acc.id),
            "scheduledAt": "2026-04-25T14:30:00Z",
            "endsAt": "2026-04-25T15:30:00Z",
            "stage": "intro",
        },
    )
    assert r.status_code == 400


async def test_create_resume_not_in_account_returns_400(client, staff_user, auth_header):
    acc1 = await _make_account(owner_id=staff_user.id, email="x@example.com")
    acc2 = await _make_account(owner_id=staff_user.id, email="y@example.com")
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(
            account=acc1,
            scheduled_at="2026-04-25T14:30:00Z",
            resume_id=str(acc2.resumes[0].id),  # belongs to acc2, not acc1
        ),
    )
    assert r.status_code == 400


async def test_update_resume_not_in_account_returns_400(client, staff_user, auth_header):
    acc1 = await _make_account(owner_id=staff_user.id, email="x@example.com")
    acc2 = await _make_account(owner_id=staff_user.id, email="y@example.com")
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc1, scheduled_at="2026-04-25T14:30:00Z"),
    )
    iv_id = r.json()["_id"]

    r = await client.put(
        f"/interviews/{iv_id}",
        headers=auth_header(staff_user),
        json={"resumeId": str(acc2.resumes[0].id)},
    )
    assert r.status_code == 400


async def test_staff_cannot_use_other_users_account(client, staff_user, staff_user_2, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user_2),
        json=_payload(account=acc, scheduled_at="2026-04-25T14:30:00Z"),
    )
    assert r.status_code == 403


async def test_admin_can_use_any_account(client, staff_user, admin_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(admin_user),
        json=_payload(account=acc, scheduled_at="2026-04-25T14:30:00Z"),
    )
    assert r.status_code == 201


async def test_staff_can_read_others_interviews(client, staff_user, staff_user_2, auth_header):
    """Spec departure: every authenticated user sees every interview."""
    acc = await _make_account(owner_id=staff_user.id)
    await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(
            account=acc,
            scheduled_at="2026-04-25T10:00:00Z",
            transcript="<p>secret content</p>",
        ),
    )
    r = await client.get("/interviews", headers=auth_header(staff_user_2))
    assert r.status_code == 200
    body = r.json()
    assert body["pagination"]["total"] == 1
    assert body["interviews"][0]["transcript"] == "<p>secret content</p>"


async def test_staff_cannot_update_others_interview(client, staff_user, staff_user_2, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc, scheduled_at="2026-04-25T10:00:00Z"),
    )
    iv_id = r.json()["_id"]

    r = await client.put(
        f"/interviews/{iv_id}",
        headers=auth_header(staff_user_2),
        json={"note": "stolen"},
    )
    assert r.status_code == 403


async def test_staff_cannot_delete_others_interview(client, staff_user, staff_user_2, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc, scheduled_at="2026-04-25T10:00:00Z"),
    )
    iv_id = r.json()["_id"]

    r = await client.delete(f"/interviews/{iv_id}", headers=auth_header(staff_user_2))
    assert r.status_code == 403


async def test_admin_can_update_any_interview(client, staff_user, admin_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc, scheduled_at="2026-04-25T10:00:00Z"),
    )
    iv_id = r.json()["_id"]

    r = await client.put(
        f"/interviews/{iv_id}",
        headers=auth_header(admin_user),
        json={"stage": "final"},
    )
    assert r.status_code == 200
    assert r.json()["stage"] == "final"


async def test_admin_can_delete_any_interview(client, staff_user, admin_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc, scheduled_at="2026-04-25T10:00:00Z"),
    )
    iv_id = r.json()["_id"]

    r = await client.delete(f"/interviews/{iv_id}", headers=auth_header(admin_user))
    assert r.status_code == 200


async def test_filter_by_stage(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    for stage in ("intro", "tech1", "final"):
        await client.post(
            "/interviews",
            headers=auth_header(staff_user),
            json=_payload(account=acc, scheduled_at="2026-04-25T10:00:00Z", stage=stage),
        )
    r = await client.get("/interviews?stage=tech1", headers=auth_header(staff_user))
    assert r.status_code == 200
    body = r.json()
    assert body["pagination"]["total"] == 1
    assert body["interviews"][0]["stage"] == "tech1"


async def test_filter_by_creator(client, staff_user, staff_user_2, auth_header):
    acc1 = await _make_account(owner_id=staff_user.id, email="a@example.com")
    acc2 = await _make_account(owner_id=staff_user_2.id, email="b@example.com")
    await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc1, scheduled_at="2026-04-25T10:00:00Z"),
    )
    await client.post(
        "/interviews",
        headers=auth_header(staff_user_2),
        json=_payload(account=acc2, scheduled_at="2026-04-25T10:00:00Z"),
    )

    r = await client.get(f"/interviews?creatorId={staff_user.id}", headers=auth_header(staff_user_2))
    assert r.status_code == 200
    body = r.json()
    assert body["pagination"]["total"] == 1
    assert body["interviews"][0]["createdBy"]["_id"] == str(staff_user.id)


async def test_filter_by_date_range(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    for dt in ("2026-01-15T10:00:00Z", "2026-04-25T10:00:00Z", "2026-08-10T10:00:00Z"):
        await client.post(
            "/interviews",
            headers=auth_header(staff_user),
            json=_payload(account=acc, scheduled_at=dt),
        )
    r = await client.get(
        "/interviews?from=2026-04-01&to=2026-05-01",
        headers=auth_header(staff_user),
    )
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1


async def test_filter_by_account(client, staff_user, auth_header):
    acc1 = await _make_account(owner_id=staff_user.id, email="one@example.com")
    acc2 = await _make_account(owner_id=staff_user.id, email="two@example.com")
    await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc1, scheduled_at="2026-04-25T10:00:00Z"),
    )
    await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(account=acc2, scheduled_at="2026-04-25T10:00:00Z"),
    )
    r = await client.get(f"/interviews?accountId={acc1.id}", headers=auth_header(staff_user))
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1


async def test_sort_asc_vs_desc(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    for dt in ("2026-01-01T10:00:00Z", "2026-06-01T10:00:00Z", "2026-12-01T10:00:00Z"):
        await client.post(
            "/interviews",
            headers=auth_header(staff_user),
            json=_payload(account=acc, scheduled_at=dt),
        )

    r = await client.get("/interviews?sort=desc", headers=auth_header(staff_user))
    dates_desc = [row["scheduledAt"] for row in r.json()["interviews"]]
    assert dates_desc == sorted(dates_desc, reverse=True)

    r = await client.get("/interviews?sort=asc", headers=auth_header(staff_user))
    dates_asc = [row["scheduledAt"] for row in r.json()["interviews"]]
    assert dates_asc == sorted(dates_asc)


async def test_accounts_lookup_returns_all_for_authenticated(client, staff_user, staff_user_2, auth_header):
    """Lookup endpoint ignores owner filter so any user can populate the filter dropdown."""
    await _make_account(owner_id=staff_user.id, name="A", email="a@example.com")
    await _make_account(owner_id=staff_user_2.id, name="B", email="b@example.com")
    r = await client.get("/accounts/lookup", headers=auth_header(staff_user))
    assert r.status_code == 200
    accounts = r.json()["accounts"]
    assert len(accounts) == 2
    emails = sorted(a["email"] for a in accounts)
    assert emails == ["a@example.com", "b@example.com"]


async def test_accounts_lookup_requires_auth(client):
    r = await client.get("/accounts/lookup")
    assert r.status_code == 401


async def test_users_lookup_returns_all_for_non_admin(client, staff_user, staff_user_2, admin_user, auth_header):
    """Non-admin staff can hit /users/lookup so they can populate the creator filter."""
    r = await client.get("/users/lookup", headers=auth_header(staff_user))
    assert r.status_code == 200
    users = r.json()["users"]
    emails = sorted(u["email"] for u in users)
    assert emails == sorted([staff_user.email, staff_user_2.email, admin_user.email])


async def test_users_lookup_requires_auth(client):
    r = await client.get("/users/lookup")
    assert r.status_code == 401
