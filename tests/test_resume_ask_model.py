"""Ask-a-screening-question on an existing job with a chosen model (real API on the test DB; LLM stubbed)."""
from app.llm_router import LlmResult
from app.models import Account, ResumeApplication

JD = "Senior Python engineer. Requirements: Python, Kafka. " * 5


async def _completed_job(owner) -> ResumeApplication:
    acc = Account(name="Backend", createdBy=owner.id, styleTemplate="<html><body><p>Jane</p></body></html>")
    await acc.insert()
    job = ResumeApplication(userId=owner.id, accountId=acc.id, companyName="Acme", companyNameLower="acme",
                            jobDescription=JD, status="completed", step="done", plainText="Jane, Python engineer")
    await job.insert()
    return job


async def test_ask_forwards_requested_model_and_records_cost(client, staff_user, auth_header, anthropic_key, monkeypatch):
    seen = {}

    async def fake(prompt, user, questions, model_id=None):
        seen["model_id"] = model_id
        return LlmResult(text='{"answers": ["Yes."]}', provider="anthropic", model="claude-opus-5-5",
                         usage={"input": 10, "output": 5}, fallback_used=False,
                         model_id="anthropic:claude-opus-5-5", cost_usd=0.002)

    monkeypatch.setattr("app.routers.resume.complete_screening", fake)
    job = await _completed_job(staff_user)
    r = await client.post(f"/resume/jobs/{job.id}/ask", headers=auth_header(staff_user),
                          json={"questions": ["Python?"], "model": "anthropic:claude-opus-5-5"})
    assert r.status_code == 200, r.text
    assert seen["model_id"] == "anthropic:claude-opus-5-5"
    fresh = await ResumeApplication.get(job.id)
    assert fresh.screeningLlmModel == "claude-opus-5-5" and fresh.estimatedCostUsd == 0.002
    assert [p.answer for p in fresh.screeningPairs] == ["Yes."]


async def test_ask_unknown_model_is_400(client, staff_user, auth_header, anthropic_key):
    job = await _completed_job(staff_user)
    r = await client.post(f"/resume/jobs/{job.id}/ask", headers=auth_header(staff_user),
                          json={"questions": ["Python?"], "model": "anthropic:nope"})
    assert r.status_code == 400 and "anthropic:nope" in r.json()["detail"]
