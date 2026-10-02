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
        "companyName": "Acme",
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
    assert row["stage"] == "tech_round_1"
    assert row["endsAt"] is not None
    assert row["resumeFilename"] == "default.md"

    r = await client.put(
        f"/interviews/{iv_id}",
        headers=auth_header(staff_user),
        json={"stage": "tech2"},
    )
    assert r.status_code == 200
    assert r.json()["stage"] == "tech_round_2"

    r = await client.delete(f"/interviews/{iv_id}", headers=auth_header(staff_user))
    assert r.status_code == 200


async def _create_intro(client, staff_user, auth_header, **extras) -> dict:
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(
            account=acc,
            scheduled_at="2026-04-25T14:30:00Z",
            stage="intro",
            status="completed",
            **extras,
        ),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_puts_transcript_note_status_on_tip_stage(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header, transcript="intro script", note="intro note")
    tip = iv["stageHistory"][-1]
    assert tip["id"]
    assert tip["stage"] == "intro"
    assert tip["status"] == "completed"
    assert tip["transcript"] == "intro script"
    assert tip["note"] == "intro note"
    assert iv["transcript"] == ""


async def test_create_empty_status_is_ok(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(
            account=acc,
            scheduled_at="2026-04-25T14:30:00Z",
            stage="intro",
            status="",
        ),
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] is None


async def test_stage_add_edit_delete_round_trip(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header, transcript="intro script")
    iv_id = iv["_id"]
    intro_id = iv["stageHistory"][0]["id"]
    h = auth_header(staff_user)

    r = await client.post(
        f"/interviews/{iv_id}/stages",
        headers=h,
        json={"stage": "tech_round_1", "scheduledAt": "2026-05-02", "status": "scheduled"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert [e["stage"] for e in body["stageHistory"]] == ["intro", "tech_round_1"]
    assert body["stage"] == "tech_round_1"
    assert body["status"] == "scheduled"
    assert body["scheduledAt"].startswith("2026-05-02")
    tech_id = body["stageHistory"][-1]["id"]

    r = await client.patch(
        f"/interviews/{iv_id}/stages/{tech_id}",
        headers=h,
        json={"note": "tech note", "status": "passed"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "passed"
    assert body["stageHistory"][-1]["note"] == "tech note"
    assert body["stageHistory"][0]["transcript"] == "intro script"
    assert body["stageHistory"][0]["note"] == ""

    # Editing a non-tip stage does not change the interview-level status.
    r = await client.patch(
        f"/interviews/{iv_id}/stages/{intro_id}",
        headers=h,
        json={"status": "failed"},
    )
    assert r.status_code == 200
    assert r.json()["stageHistory"][0]["status"] == "rejected"
    assert r.json()["status"] == "passed"
    assert r.json()["stage"] == "tech_round_1"

    r = await client.delete(f"/interviews/{iv_id}/stages/{tech_id}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["stage"] for e in body["stageHistory"]] == ["intro"]
    assert body["stage"] == "intro"

    r = await client.delete(f"/interviews/{iv_id}/stages/{intro_id}", headers=h)
    assert r.status_code == 400


async def test_stage_cannot_repeat_current_stage(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    r = await client.post(
        f"/interviews/{iv['_id']}/stages",
        headers=auth_header(staff_user),
        json={"stage": "intro", "scheduledAt": "2026-05-02"},
    )
    assert r.status_code == 400


async def test_stage_endpoints_forbidden_for_other_users(client, staff_user, staff_user_2, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    r = await client.post(
        f"/interviews/{iv['_id']}/stages",
        headers=auth_header(staff_user_2),
        json={"stage": "final"},
    )
    assert r.status_code == 403


async def test_get_migrates_legacy_interview_fields(client, staff_user, auth_header):
    from app.models import Interview
    from app.models.interview import InterviewStage, InterviewStatus

    acc = await _make_account(owner_id=staff_user.id)
    legacy = Interview(
        accountId=acc.id,
        createdBy=staff_user.id,
        scheduledAt=datetime(2026, 4, 25, 14, 30),
        stage=InterviewStage.intro,
        status=InterviewStatus.completed,
        transcript="old script",
        note="old note",
        stageHistory=[],
    )
    await legacy.insert()

    r = await client.get(f"/interviews/{legacy.id}", headers=auth_header(staff_user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"] == ""
    tip = body["stageHistory"][-1]
    assert tip["transcript"] == "old script"
    assert tip["note"] == "old note"
    assert tip["status"] == "completed"

    r2 = await client.get(f"/interviews/{legacy.id}", headers=auth_header(staff_user))
    assert r2.json()["stageHistory"][-1]["id"] == tip["id"]


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
    assert body["interviews"][0]["stageHistory"][-1]["transcript"] == "<p>secret content</p>"


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
    assert body["interviews"][0]["stage"] == "tech_round_1"


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


async def test_caller_mode_create_and_coworker_can_view(
    client, staff_user, staff_user_2, admin_user, make_user, auth_header
):
    outsider = await make_user(name="Outsider", email="out@example.com")
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(
            account=acc,
            scheduled_at="2026-09-30T01:15:00Z",
            caller={
                "enabled": True,
                "timezone": "America/New_York",
                "method": "video",
                "methodValue": "https://meet.example/abc",
                "coworkerIds": [str(staff_user_2.id), str(staff_user.id)],
            },
        ),
    )
    assert r.status_code == 201, r.text
    caller = r.json()["caller"]
    assert caller["enabled"] is True
    assert caller["callerName"] == "TBD"
    assert caller["startsAt"].startswith("2026-09-30T01:15:00")
    assert caller["coworkerIds"] == [str(staff_user_2.id)]
    iv_id = r.json()["_id"]

    r = await client.get(f"/interviews/{iv_id}", headers=auth_header(staff_user_2))
    assert r.status_code == 200
    assert [c["name"] for c in r.json()["caller"]["coworkers"]] == ["Staff Two"]

    r = await client.get(f"/interviews/{iv_id}", headers=auth_header(outsider))
    assert r.status_code == 403

    r = await client.put(f"/interviews/{iv_id}", headers=auth_header(staff_user_2), json={"note": "x"})
    assert r.status_code == 403

    r = await client.get(f"/interviews/{iv_id}", headers=auth_header(staff_user))
    tip_id = r.json()["stageHistory"][-1]["id"]
    r = await client.patch(
        f"/interviews/{iv_id}/stages/{tip_id}",
        headers=auth_header(staff_user),
        json={"caller": {"enabled": False}},
    )
    assert r.status_code == 200
    assert r.json()["caller"]["enabled"] is False
    assert r.json()["stageHistory"][-1]["caller"]["enabled"] is False
    r = await client.get(f"/interviews/{iv_id}", headers=auth_header(staff_user_2))
    assert r.status_code == 403


async def test_caller_mode_rejects_unknown_coworker(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(
            account=acc, scheduled_at="2026-09-29T12:00:00",
            caller={"enabled": True, "coworkerIds": ["64b7f0000000000000000000"]},
        ),
    )
    assert r.status_code == 400, r.text


async def test_caller_mode_ignores_timezone_from_old_clients(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post(
        "/interviews",
        headers=auth_header(staff_user),
        json=_payload(
            account=acc, scheduled_at="2026-09-29T12:00:00",
            caller={"enabled": True, "timezone": "Mars/Olympus"},
        ),
    )
    assert r.status_code == 201, r.text
    assert r.json()["caller"]["timezone"] is None


async def test_put_legacy_rejected_scheduled_sets_canceled_without_new_round(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    r = await client.put(
        f"/interviews/{iv['_id']}",
        headers=auth_header(staff_user),
        json={"stage": "rejected", "status": "scheduled", "scheduledAt": "2026-05-01T10:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "canceled"
    assert body["stage"] == "intro"
    assert len(body["stageHistory"]) == 1


async def test_put_legacy_failed_status_maps_to_rejected(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    r = await client.put(
        f"/interviews/{iv['_id']}",
        headers=auth_header(staff_user),
        json={"status": "failed"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


async def test_post_stage_rejected_returns_400(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    r = await client.post(
        f"/interviews/{iv['_id']}/stages",
        headers=auth_header(staff_user),
        json={"stage": "rejected", "scheduledAt": "2026-05-01T10:00:00Z"},
    )
    assert r.status_code == 400


async def test_saving_unmigrated_interview_relinks_questions(client, staff_user, auth_header):
    from bson import ObjectId

    from app.models import Interview, InterviewQuestion

    acc = await _make_account(owner_id=staff_user.id)
    when = datetime(2026, 4, 25, 14, 30)
    oid = ObjectId()
    await Interview.get_motor_collection().insert_one({
        "_id": oid, "accountId": acc.id, "createdBy": staff_user.id, "scheduledAt": when,
        "stage": "rejected", "status": "scheduled", "transcript": "", "note": "",
        "stageHistory": [
            {"id": "a", "stage": "intro", "status": "completed", "at": when, "scheduledAt": when},
            {"id": "b", "stage": "rejected", "status": "scheduled", "at": when, "scheduledAt": when},
        ],
    })
    await InterviewQuestion.get_motor_collection().insert_one(
        {"interviewId": oid, "userId": staff_user.id, "stageId": "b", "stage": "rejected", "question": "Q?"},
    )
    r = await client.patch(f"/interviews/{oid}/stages/a", headers=auth_header(staff_user), json={"note": "n"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "canceled"
    q = await InterviewQuestion.get_motor_collection().find_one({"interviewId": oid})
    assert (q["stageId"], q["stage"]) == ("a", "intro")


async def test_stage_move_from_canceled_starts_new_round_scheduled(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    h = auth_header(staff_user)
    r = await client.put(f"/interviews/{iv['_id']}", headers=h, json={"status": "canceled"})
    assert r.json()["status"] == "canceled"
    r = await client.put(
        f"/interviews/{iv['_id']}", headers=h,
        json={"stage": "tech_round_1", "scheduledAt": "2026-05-02T10:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage"] == "tech_round_1"
    assert body["status"] == "scheduled"
    assert body["stageHistory"][0]["status"] == "canceled"


async def test_create_with_round_shape(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post("/interviews", headers=auth_header(staff_user), json={
        "accountId": str(acc.id),
        "companyName": "Globex",
        "round": {
            "stage": "intro", "scheduledAt": "2026-10-01T14:00:00Z", "endsAt": "2026-10-01T14:45:00Z",
            "interviewerName": "Dana", "note": "n",
        },
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["companyName"] == "Globex"
    assert body["status"] == "scheduled"
    tip = body["stageHistory"][-1]
    assert (tip["stage"], tip["interviewerName"], tip["note"]) == ("intro", "Dana", "n")
    assert tip["endsAt"].startswith("2026-10-01T14:45")
    assert body["interviewerName"] == "Dana"  # top-level mirror


async def test_create_requires_company(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    r = await client.post("/interviews", headers=auth_header(staff_user), json={
        "accountId": str(acc.id), "companyName": " ",
        "round": {"stage": "intro", "scheduledAt": "2026-10-01T14:00:00Z"},
    })
    assert r.status_code == 400


async def test_create_legacy_flat_shape_still_works(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header, interviewerName="Lee")
    assert iv["stageHistory"][-1]["interviewerName"] == "Lee"
    assert iv["stageHistory"][-1]["status"] == "completed"


async def test_put_with_stage_history_returns_400(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    r = await client.put(
        f"/interviews/{iv['_id']}", headers=auth_header(staff_user),
        json={"stageHistory": [{"stage": "intro"}]},
    )
    assert r.status_code == 400


async def test_add_round_mark_previous_passed(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    r = await client.post(
        f"/interviews/{iv['_id']}/stages", headers=auth_header(staff_user),
        json={"stage": "tech_round_1", "scheduledAt": "2026-05-02T10:00:00Z", "markPreviousPassed": True},
    )
    assert r.status_code == 201, r.text
    statuses = [e["status"] for e in r.json()["stageHistory"]]
    assert statuses == ["passed", "scheduled"]


async def test_add_round_keeps_rejected_previous_when_mark_true(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    h = auth_header(staff_user)
    await client.put(f"/interviews/{iv['_id']}", headers=h, json={"status": "rejected"})
    r = await client.post(
        f"/interviews/{iv['_id']}/stages", headers=h,
        json={"stage": "tech_round_1", "scheduledAt": "2026-05-02T10:00:00Z", "markPreviousPassed": True},
    )
    assert [e["status"] for e in r.json()["stageHistory"]] == ["rejected", "scheduled"]


async def test_patch_round_interviewer_and_caller(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    tip_id = iv["stageHistory"][-1]["id"]
    r = await client.patch(
        f"/interviews/{iv['_id']}/stages/{tip_id}", headers=auth_header(staff_user),
        json={"interviewerName": "Kim", "caller": {"enabled": True, "callerName": "Sam"}},
    )
    assert r.status_code == 200, r.text
    tip = r.json()["stageHistory"][-1]
    assert tip["interviewerName"] == "Kim"
    assert tip["caller"]["callerName"] == "Sam"
    assert r.json()["caller"]["callerName"] == "Sam"


async def test_patch_round_date_moves_caller_start(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header, caller={"enabled": True})
    tip_id = iv["stageHistory"][-1]["id"]
    r = await client.patch(
        f"/interviews/{iv['_id']}/stages/{tip_id}", headers=auth_header(staff_user),
        json={"scheduledAt": "2026-05-03T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["stageHistory"][-1]["caller"]["startsAt"].startswith("2026-05-03T16:00")


async def test_list_date_range_matches_any_round(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)  # intro on 2026-04-25
    h = auth_header(staff_user)
    await client.post(f"/interviews/{iv['_id']}/stages", headers=h,
                      json={"stage": "tech_round_1", "scheduledAt": "2026-06-10T10:00:00Z"})
    r = await client.get("/interviews?from=2026-04-20&to=2026-04-30", headers=h)
    assert r.json()["pagination"]["total"] == 1  # only the earlier round is in range


async def test_list_sort_company_asc(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    h = auth_header(staff_user)
    for name in ("Zeta", "Alpha", "Mid"):
        await client.post("/interviews", headers=h, json={
            "accountId": str(acc.id), "companyName": name,
            "round": {"stage": "intro", "scheduledAt": "2026-04-25T10:00:00Z"},
        })
    r = await client.get("/interviews?sort=company&dir=asc", headers=h)
    assert [i["companyName"] for i in r.json()["interviews"]] == ["Alpha", "Mid", "Zeta"]


async def test_rounds_endpoint_requires_range(client, staff_user, auth_header):
    r = await client.get("/interviews/rounds", headers=auth_header(staff_user))
    assert r.status_code == 400
    r = await client.get("/interviews/rounds?from=2026-01-01&to=2026-06-01", headers=auth_header(staff_user))
    assert r.status_code == 400  # more than 45 days


async def test_rounds_endpoint_route_not_shadowed_by_id(client, staff_user, auth_header):
    await _create_intro(client, staff_user, auth_header)
    r = await client.get("/interviews/rounds?from=2026-04-20&to=2026-04-27", headers=auth_header(staff_user))
    assert r.status_code == 200, r.text
    assert [row["stage"] for row in r.json()["rounds"]] == ["intro"]


async def test_list_sort_company_is_case_insensitive(client, staff_user, auth_header):
    acc = await _make_account(owner_id=staff_user.id)
    h = auth_header(staff_user)
    for name in ("zeta", "Alpha", "beta"):
        await client.post("/interviews", headers=h, json={
            "accountId": str(acc.id), "companyName": name,
            "round": {"stage": "intro", "scheduledAt": "2026-04-25T10:00:00Z"},
        })
    r = await client.get("/interviews?sort=company&dir=asc", headers=h)
    assert [i["companyName"] for i in r.json()["interviews"]] == ["Alpha", "beta", "zeta"]


async def test_create_and_put_return_populated_interview(client, staff_user, auth_header):
    iv = await _create_intro(client, staff_user, auth_header)
    assert isinstance(iv["accountId"], dict) and iv["accountId"]["email"] == "acme@example.com"
    r = await client.put(f"/interviews/{iv['_id']}", headers=auth_header(staff_user), json={"jobUrl": "https://x.test/j"})
    assert r.json()["accountId"]["email"] == "acme@example.com"
    assert r.json()["ownerEmail"] == staff_user.email
