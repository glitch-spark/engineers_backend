"""Integration: /resume/eval JD library and runs against a real Mongo (MONGODB_URI_TEST).

The runner is stubbed: these tests cover persistence, validation and ownership, not the model calls.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import model_catalog as mc
from app.models import Account, EvalJobDescription, ResumePromptEval
from app.prompt_eval import runner
from app.prompt_eval.seed_jds import SEED_JDS, seed_eval_jds
from app.routers import prompt_eval as pe

TEMPLATE = "<html><head><style>.x{}</style></head><body><p>Hi</p></body></html>"


@pytest.fixture(autouse=True)
def models_configured(monkeypatch):
    monkeypatch.setattr(
        mc, "get_settings",
        lambda: SimpleNamespace(
            ANTHROPIC_API_KEY="sk-ant-test", OPENAI_API_KEY=None,
            RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5", COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5",
            SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5", JUDGE_DEFAULT_MODEL="anthropic:claude-opus-5-5",
        ),
    )


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(pe.runner, "start_run", lambda run_id: calls.append(run_id))
    return calls


async def make_account(user, **over):
    acc = Account(name=over.pop("name", "Profile"), createdBy=user.id, styleTemplate=over.pop("styleTemplate", TEMPLATE), **over)
    await acc.insert()
    return acc


async def test_seed_present_after_boot_and_idempotent(client, staff_user, auth_header):
    assert await seed_eval_jds() == len(SEED_JDS)  # db_clean wiped the table; the boot-time seed re-runs cleanly
    assert await seed_eval_jds() == 0
    r = await client.get("/resume/eval/jds", headers=auth_header(staff_user))
    assert r.status_code == 200
    body = r.json()
    assert len(body["jds"]) == len(SEED_JDS) and body["roles"][0] == "fullstack"
    roles = body["roles"]
    assert [j["role"] for j in body["jds"]] == sorted((j["role"] for j in body["jds"]), key=roles.index)  # role order
    assert all(j["seedKey"] for j in body["jds"])


async def test_jd_crud_and_archive(client, staff_user, auth_header):
    h = auth_header(staff_user)
    r = await client.post("/resume/eval/jds", json={"role": "ai", "title": "AI Eng", "company": "Co", "text": "Do AI things."}, headers=h)
    assert r.status_code == 201
    jd_id = r.json()["_id"]
    assert r.json()["createdBy"] == str(staff_user.id) and r.json()["archived"] is False

    r = await client.put(f"/resume/eval/jds/{jd_id}", json={"title": "AI Engineer II"}, headers=h)
    assert r.status_code == 200 and r.json()["title"] == "AI Engineer II"

    r = await client.post(f"/resume/eval/jds/{jd_id}/archive", headers=h)
    assert r.json()["archived"] is True and r.json()["archivedAt"]
    r = await client.get("/resume/eval/jds", headers=h)
    assert jd_id not in [j["_id"] for j in r.json()["jds"]]
    r = await client.get("/resume/eval/jds", params={"includeArchived": "true"}, headers=h)
    assert jd_id in [j["_id"] for j in r.json()["jds"]]

    r = await client.post(f"/resume/eval/jds/{jd_id}/unarchive", headers=h)
    assert r.json()["archived"] is False and r.json()["archivedAt"] is None


async def test_create_run_persists_rows_and_starts_the_runner(client, staff_user, auth_header, started):
    await seed_eval_jds()
    acc = await make_account(staff_user, resumePromptBody="Profile prompt")
    body = {"accountId": str(acc.id), "roles": ["ai", "data"], "label": "v1", "judgeModel": "anthropic:claude-opus-5-5"}
    r = await client.post("/resume/eval/runs", json=body, headers=auth_header(staff_user))
    assert r.status_code == 202, r.text
    assert r.json()["jdCount"] == 2 and r.json()["status"] == "running"
    run = await ResumePromptEval.get(r.json()["runId"])
    assert started == [run.id]
    assert run.promptSource == "profile" and run.promptBody == "Profile prompt" and run.label == "v1"
    assert run.resumeModelId is None and run.judgeModelId == "anthropic:claude-opus-5-5"
    assert [row.role for row in run.rows] == ["ai", "data"] and all(row.status == "pending" for row in run.rows)
    assert run.counts.total == 2 and run.accountName == "Profile"

    # A custom prompt (even empty) wins over the saved one and is snapshotted.
    r = await client.post("/resume/eval/runs", json={"accountId": str(acc.id), "jdIds": [str(run.rows[0].jdId)], "promptBody": ""}, headers=auth_header(staff_user))
    assert r.status_code == 202
    run2 = await ResumePromptEval.get(r.json()["runId"])
    assert run2.promptSource == "custom" and run2.promptBody == "" and len(run2.rows) == 1


async def test_create_run_rejects_archived_or_unknown_jd_ids(client, staff_user, auth_header, started):
    await seed_eval_jds()
    acc = await make_account(staff_user)
    jd = await EvalJobDescription.find_one({"role": "ml"})
    jd.archived = True
    await jd.save()
    h = auth_header(staff_user)
    r = await client.post("/resume/eval/runs", json={"accountId": str(acc.id), "jdIds": [str(jd.id)]}, headers=h)
    assert r.status_code == 400 and "archived" in r.json()["detail"].lower()
    r = await client.post("/resume/eval/runs", json={"accountId": str(acc.id), "jdIds": ["000000000000000000000001"]}, headers=h)
    assert r.status_code == 400 and "unknown" in r.json()["detail"].lower()
    assert started == []


async def test_other_users_profile_is_404_and_runs_are_private(client, staff_user, staff_user_2, admin_user, auth_header, started):
    await seed_eval_jds()
    acc = await make_account(staff_user)
    r = await client.post("/resume/eval/runs", json={"accountId": str(acc.id), "roles": ["ai"]}, headers=auth_header(staff_user_2))
    assert r.status_code == 404
    r = await client.post("/resume/eval/runs", json={"accountId": str(acc.id), "roles": ["ai"]}, headers=auth_header(staff_user))
    run_id = r.json()["runId"]
    assert (await client.get(f"/resume/eval/runs/{run_id}", headers=auth_header(staff_user_2))).status_code == 404
    assert (await client.get(f"/resume/eval/runs/{run_id}", headers=auth_header(admin_user))).status_code == 200
    mine = (await client.get("/resume/eval/runs", headers=auth_header(staff_user))).json()
    theirs = (await client.get("/resume/eval/runs", headers=auth_header(staff_user_2))).json()
    assert [x["_id"] for x in mine["runs"]] == [run_id] and theirs["runs"] == []


async def test_list_marks_stale_runs_failed_and_strips_resume_text(client, staff_user, auth_header, started):
    await seed_eval_jds()
    acc = await make_account(staff_user)
    r = await client.post("/resume/eval/runs", json={"accountId": str(acc.id), "roles": ["ai"]}, headers=auth_header(staff_user))
    run = await ResumePromptEval.get(r.json()["runId"])
    run.startedAt = datetime.utcnow() - runner.STALE_AFTER - timedelta(minutes=1)
    run.rows[0].resumeText = "secret long text"
    run.rows[0].resumeHtml = "<html/>"
    await run.save()
    listed = (await client.get("/resume/eval/runs", headers=auth_header(staff_user))).json()["runs"][0]
    assert listed["status"] == "failed" and "timed out" in listed["error"]
    assert "resumeText" not in listed["rows"][0] and "resumeHtml" not in listed["rows"][0]
    full = (await client.get(f"/resume/eval/runs/{run.id}", headers=auth_header(staff_user))).json()
    assert full["rows"][0]["resumeText"] == "secret long text"
    assert (await ResumePromptEval.get(run.id)).status == "failed"


async def test_delete_run(client, staff_user, auth_header, started):
    await seed_eval_jds()
    acc = await make_account(staff_user)
    r = await client.post("/resume/eval/runs", json={"accountId": str(acc.id), "roles": ["test"]}, headers=auth_header(staff_user))
    run_id = r.json()["runId"]
    r = await client.delete(f"/resume/eval/runs/{run_id}", headers=auth_header(staff_user))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert await ResumePromptEval.get(run_id) is None
