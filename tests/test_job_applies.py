"""Job Applies API lifecycle against a real test database. The worker is driven directly with fake
fetch/extract functions, so no network or LLM is used."""
import io
import json
from datetime import date, datetime, timedelta

import pytest
from beanie import PydanticObjectId
from openpyxl import Workbook

from app.job_apply import worker
from app.job_apply.fetch import FetchError, FetchResult
from app.job_apply.schema import JobExtraction, Location, MinYears, Term
from app.models import Account
from app.models.account import Resume

REMOTE_JD = "Senior Python Engineer\nRequirements\n- 3+ years Python\n- Kafka\nFully remote within the US. " * 3
HYBRID_JD = "Python Engineer\nRequirements\n- Python\nThis is a hybrid role, 3 days in office. " * 3
STRONG = "Senior Python Engineer EXPERIENCE Acme Senior Python Engineer Jan 2019 - Present Python Kafka services."
WEAK = "Designer EXPERIENCE Studio Designer Jan 2024 - Present Figma."


async def fake_extract(jd, *, user, run_date, llm=None):
    hybrid = "hybrid" in jd
    return JobExtraction(
        title="Python Engineer" if hybrid else "Senior Python Engineer",
        seniority=None if hybrid else "senior",
        terms=[Term(term="Python", tier="required"), Term(term="Kafka", tier="required")],
        minYears=MinYears(value=3, required=True),
        workMode="hybrid" if hybrid else "remote",
        allowedLocations=[Location(kind="country", value="US")],
        postedDate=date(2026, 9, 25),
    ), "llm"


async def fake_fetch(url, *, client, browser=None):
    return FetchResult(text=REMOTE_JD, source="ats_api", postedDate=date(2026, 9, 28), title="Fetched Role")


async def failing_fetch(url, *, client, browser=None):
    raise FetchError("no job text on the page")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(worker, "EXTRACT", fake_extract)
    monkeypatch.setattr(worker, "FETCH", fake_fetch)


def sheet(rows) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


SHEET = sheet([
    ["URL", "Description"],
    ["https://jobs.example.com/remote", REMOTE_JD],
    ["https://jobs.example.com/hybrid", HYBRID_JD],
    ["https://boards.greenhouse.io/acme/jobs/9", None],
])


async def make_account(owner, *, country="US", resumes=(("strong.md", STRONG), ("weak.md", WEAK)), name="Backend") -> Account:
    acc = Account(
        name=name, country=country, region="US" if country == "US" else "Latam", createdBy=owner.id,
        resumes=[Resume(filename=f, markdown=m) for f, m in resumes],
        styleTemplate="<html><body><h1>Jane</h1><p>Engineer</p></body></html>",
    )
    await acc.insert()
    return acc


async def create_run(client, headers, acc, *, data=SHEET, threshold=60, resume_ids=None):
    selection = [{"accountId": str(acc.id), "resumeIds": resume_ids or []}]
    return await client.post(
        "/job-applies/runs",
        headers=headers,
        files={"file": ("jobs.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"selection": json.dumps(selection), "threshold": str(threshold), "maxAgeDays": "30"},
    )


async def test_create_run_and_results(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    r = await create_run(client, h, acc)
    assert r.status_code == 202, r.text
    run_id = r.json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))

    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["status"] == "done" and run["counts"]["total"] == 3 and run["counts"]["fetched"] == 1
    assert run["profiles"][0]["name"] == "Backend"

    rows = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    by_url = {x["url"]: x for x in rows}
    remote = by_url["https://jobs.example.com/remote"]
    assert remote["status"] == "scored" and len(remote["suggestions"]) == 1
    assert remote["suggestions"][0]["filename"] == "strong.md" and "jdText" not in remote and "scores" not in remote
    assert by_url["https://jobs.example.com/hybrid"]["status"] == "excluded"
    assert by_url["https://jobs.example.com/hybrid"]["suggestions"] == []
    fetched = by_url["https://boards.greenhouse.io/acme/jobs/9"]
    assert fetched["jdSource"] == "ats_api" and fetched["postedDate"].startswith("2026-09-28")

    suggested = (await client.get(f"/job-applies/runs/{run_id}/rows?view=suggested", headers=h)).json()
    assert {x["url"] for x in suggested["rows"]} == {"https://jobs.example.com/remote", "https://boards.greenhouse.io/acme/jobs/9"}
    excluded = (await client.get(f"/job-applies/runs/{run_id}/rows?view=excluded", headers=h)).json()
    assert [x["url"] for x in excluded["rows"]] == ["https://jobs.example.com/hybrid"]

    detail = (await client.get(f"/job-applies/rows/{remote['_id']}", headers=h)).json()
    assert len(detail["scores"]) == 2 and detail["scores"][0]["components"]


async def test_threshold_patch_changes_suggestions(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id = (await create_run(client, h, acc)).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    r = await client.patch(f"/job-applies/runs/{run_id}", headers=h, json={"threshold": 100})
    assert r.status_code == 200 and r.json()["threshold"] == 100
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    assert all(x["suggestions"] == [] for x in rows)


async def test_location_gate_excludes_profile(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user, country="BR")
    run_id = (await create_run(client, h, acc)).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    remote = next(x for x in rows if x["url"].endswith("/remote"))
    assert remote["suggestions"] == [] and remote["profileGates"][0]["gates"][0]["result"] == "fail"


async def test_cancel_and_retry_failed(client, staff_user, auth_header, monkeypatch):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    monkeypatch.setattr(worker, "FETCH", failing_fetch)
    run_id = (await create_run(client, h, acc)).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["counts"]["failed"] == 1
    failed = (await client.get(f"/job-applies/runs/{run_id}/rows?view=failed", headers=h)).json()["rows"]
    assert len(failed) == 1 and failed[0]["status"] == "fetch_failed" and "no job text" in failed[0]["statusReason"]

    monkeypatch.setattr(worker, "FETCH", fake_fetch)
    r = await client.post(f"/job-applies/runs/{run_id}/retry-failed", headers=h)
    assert r.status_code == 200 and r.json()["reset"] == 1
    await worker.run_worker(PydanticObjectId(run_id))
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["status"] == "done" and run["counts"]["failed"] == 0

    queued = (await create_run(client, h, acc)).json()["runId"]
    r = await client.post(f"/job-applies/runs/{queued}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    await worker.run_worker(PydanticObjectId(queued))  # no-op on a cancelled run
    assert (await client.get(f"/job-applies/runs/{queued}", headers=h)).json()["status"] == "cancelled"
    assert (await client.post(f"/job-applies/runs/{queued}/cancel", headers=h)).status_code == 409


async def test_second_active_run_409(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    assert (await create_run(client, h, acc)).status_code == 202
    r = await create_run(client, h, acc)
    assert r.status_code == 409


async def test_cannot_select_foreign_account_404(client, staff_user, staff_user_2, auth_header):
    acc = await make_account(staff_user_2)
    r = await create_run(client, auth_header(staff_user), acc)
    assert r.status_code == 404


async def test_bad_file_400(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    r = await create_run(client, h, acc, data=b"not a spreadsheet")
    assert r.status_code == 400 and "spreadsheet" in r.json()["error"]
    r = await create_run(client, h, acc, data=sheet([["a", "b"]]))
    assert r.status_code == 400 and "URL" in r.json()["error"]


async def test_runs_list_and_delete(client, staff_user, staff_user_2, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id = (await create_run(client, h, acc)).json()["runId"]
    assert (await client.delete(f"/job-applies/runs/{run_id}", headers=h)).status_code == 409  # still queued
    await worker.run_worker(PydanticObjectId(run_id))
    runs = (await client.get("/job-applies/runs", headers=h)).json()["runs"]
    assert [x["_id"] for x in runs] == [run_id] and runs[0]["suggested"] >= 1
    assert (await client.get(f"/job-applies/runs/{run_id}", headers=auth_header(staff_user_2))).status_code == 404
    assert (await client.delete(f"/job-applies/runs/{run_id}", headers=h)).status_code == 200
    assert (await client.get(f"/job-applies/runs/{run_id}", headers=h)).status_code == 404


async def test_cancel_mid_run_stops_work_and_delete_sticks(client, staff_user, auth_header, monkeypatch):
    from app.models.job_apply import JobApplyRow, JobApplyRun

    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    data = sheet([["URL"]] + [[f"https://jobs.example.com/{i}"] for i in range(60)])
    run_id = (await create_run(client, h, acc, data=data)).json()["runId"]
    calls = []

    async def cancelling_fetch(url, *, client, browser=None):
        calls.append(url)
        if len(calls) == 1:
            await JobApplyRun.get_motor_collection().update_one(
                {"_id": PydanticObjectId(run_id)}, {"$set": {"status": "cancelled"}}
            )
        return await fake_fetch(url, client=client)

    monkeypatch.setattr(worker, "FETCH", cancelling_fetch)
    monkeypatch.setattr(worker, "CHECK_EVERY", 5)
    await worker.run_worker(PydanticObjectId(run_id))

    assert len(calls) <= 5 + worker.FETCH_CONCURRENCY
    assert await JobApplyRow.find({"runId": PydanticObjectId(run_id), "status": {"$in": ["extracted", "scored"]}}).count() == 0
    assert (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["status"] == "cancelled"
    assert (await client.delete(f"/job-applies/runs/{run_id}", headers=h)).status_code == 200
    assert await JobApplyRow.find({"runId": PydanticObjectId(run_id)}).count() == 0


async def test_applied_set_during_run_is_kept(client, staff_user, auth_header, monkeypatch):
    from app.models.job_apply import JobApplyRow

    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id = (await create_run(client, h, acc)).json()["runId"]

    async def extract_and_mark(jd, *, user, run_date, llm=None):
        # The user ticks "applied" while the worker is mid-run.
        await JobApplyRow.get_motor_collection().update_many(
            {"runId": PydanticObjectId(run_id)},
            {"$push": {"appliedResumes": {"accountId": "x", "resumeId": "y", "at": datetime.utcnow()}}},
        )
        return await fake_extract(jd, user=user, run_date=run_date)

    monkeypatch.setattr(worker, "EXTRACT", extract_and_mark)
    await worker.run_worker(PydanticObjectId(run_id))
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    assert rows and all(r["applied"] for r in rows)


async def test_run_detail_includes_resume_health(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id = (await create_run(client, h, acc)).json()["runId"]
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    by_name = {r["filename"]: r for r in run["resumes"]}
    assert set(by_name) == {"strong.md", "weak.md"}
    assert by_name["weak.md"]["accountId"] == str(acc.id)
    assert "no_email" in by_name["weak.md"]["health"]["issues"] and by_name["weak.md"]["health"]["score"] < 100


GSHEET = "https://docs.google.com/spreadsheets/d/1AbC_dEf-123456/edit#gid=0"


async def test_create_run_from_google_sheet(client, staff_user, auth_header, monkeypatch):
    from app.job_apply import gsheet
    from app.job_apply.sheet import parse_sheet

    async def fake_download(url, *, client):
        assert url == GSHEET
        return "Jobs Oct - Sheet1", parse_sheet("s.xlsx", SHEET)

    monkeypatch.setattr(gsheet, "download_gsheet", fake_download)
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    r = await client.post(
        "/job-applies/runs", headers=h,
        data={"sheetUrl": GSHEET, "selection": json.dumps([{"accountId": str(acc.id), "resumeIds": []}]), "threshold": "60"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["total"] == 3
    run = (await client.get(f"/job-applies/runs/{r.json()['runId']}", headers=h)).json()
    assert run["fileName"] == "Jobs Oct - Sheet1" and run["sourceUrl"] == GSHEET


async def test_google_sheet_link_errors(client, staff_user, auth_header, monkeypatch):
    from app.job_apply import gsheet
    from app.job_apply.sheet import SheetError

    async def private(url, *, client):
        raise SheetError(gsheet.PRIVATE_HINT)

    monkeypatch.setattr(gsheet, "download_gsheet", private)
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    sel = json.dumps([{"accountId": str(acc.id), "resumeIds": []}])
    r = await client.post("/job-applies/runs", headers=h, data={"sheetUrl": GSHEET, "selection": sel})
    assert r.status_code == 400 and "Anyone with the link" in r.json()["error"]
    r = await client.post("/job-applies/runs", headers=h, data={"selection": sel})
    assert r.status_code == 400 and "Google Sheets link" in r.json()["error"]


async def test_sheet_descriptions_are_cached_per_url_for_later_runs(client, staff_user, auth_header, monkeypatch):
    from app.models.job_apply import JobApplyRow, JobPostingCache

    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    first = (await create_run(client, h, acc)).json()["runId"]
    await worker.run_worker(PydanticObjectId(first))
    cached = await JobPostingCache.find_one(JobPostingCache.url == "https://jobs.example.com/remote")
    assert cached is not None and cached.source == "sheet" and "Python" in cached.text

    monkeypatch.setattr(worker, "FETCH", failing_fetch)  # a link-only row must not be fetched again
    second = (await create_run(client, h, acc, data=sheet([["URL"], ["https://jobs.example.com/remote"]]))).json()["runId"]
    await worker.run_worker(PydanticObjectId(second))
    row = await JobApplyRow.find_one(JobApplyRow.runId == PydanticObjectId(second))
    assert row.status == "scored" and row.jdSource == "sheet"



async def _done_run(client, h, acc, data=SHEET):
    run_id = (await create_run(client, h, acc, data=data)).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    return run_id, {r["url"]: r for r in rows}


async def _mark(client, h, row, *, applied=True, suggestion=0):
    s = row["suggestions"][suggestion]
    return await client.put(
        f"/job-applies/rows/{row['_id']}/applied", headers=h,
        json={"accountId": s["accountId"], "resumeId": s["resumeId"], "applied": applied},
    )


async def test_mark_resume_applied_writes_the_permanent_log(client, staff_user, auth_header):
    from app.models.job_apply import JobApplicationLog

    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id, rows = await _done_run(client, h, acc)
    remote = rows["https://jobs.example.com/remote"]
    r = await _mark(client, h, remote)
    assert r.status_code == 200 and [m["resumeId"] for m in r.json()["appliedResumes"]] == [remote["suggestions"][0]["resumeId"]]
    assert (await _mark(client, h, remote)).status_code == 200  # idempotent
    (log,) = await JobApplicationLog.find_all().to_list()
    assert log.url == remote["url"] and log.profileName == "Backend" and log.filename == "strong.md" and str(log.runId) == run_id

    rows_now = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    marked = next(x for x in rows_now if x["_id"] == remote["_id"])
    assert marked["applied"] is True and len(marked["appliedResumes"]) == 1

    r = await _mark(client, h, remote, applied=False)
    assert r.status_code == 200 and r.json()["appliedResumes"] == []
    assert await JobApplicationLog.find_all().count() == 0


async def test_applied_filters_and_counters(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id, rows = await _done_run(client, h, acc)
    await _mark(client, h, rows["https://jobs.example.com/remote"])

    def urls(resp):
        return {x["url"] for x in resp.json()["rows"]}

    yes = await client.get(f"/job-applies/runs/{run_id}/rows?applied=yes", headers=h)
    no = await client.get(f"/job-applies/runs/{run_id}/rows?view=suggested&applied=no", headers=h)
    assert urls(yes) == {"https://jobs.example.com/remote"}
    assert urls(no) == {"https://boards.greenhouse.io/acme/jobs/9"}

    since = (datetime.utcnow() - timedelta(hours=1)).isoformat() + "Z"
    run = (await client.get(f"/job-applies/runs/{run_id}", params={"appliedSince": since}, headers=h)).json()
    assert run["appliedInRun"] == 1 and run["appliedSince"] == 1
    assert all("hasFile" in r for r in run["resumes"])


async def test_later_runs_show_already_applied(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    _, rows = await _done_run(client, h, acc)
    await _mark(client, h, rows["https://jobs.example.com/remote"])

    run2, rows2 = await _done_run(client, h, acc)
    prev = rows2["https://jobs.example.com/remote"]["previouslyApplied"]
    assert len(prev) == 1 and prev[0]["profileName"] == "Backend" and prev[0]["filename"] == "strong.md"
    no = (await client.get(f"/job-applies/runs/{run2}/rows?applied=no", headers=h)).json()["rows"]
    assert "https://jobs.example.com/remote" not in {x["url"] for x in no}


async def test_bulk_mark_top_suggestion(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id, rows = await _done_run(client, h, acc)
    ids = [rows[u]["_id"] for u in ("https://jobs.example.com/remote", "https://jobs.example.com/hybrid")]
    r = await client.post(f"/job-applies/runs/{run_id}/mark-top-applied", headers=h, json={"rowIds": ids})
    assert r.status_code == 200 and r.json()["marked"] == 1  # the hybrid job has no suggestion
    again = await client.post(f"/job-applies/runs/{run_id}/mark-top-applied", headers=h, json={"rowIds": ids})
    assert again.json()["marked"] == 0


async def test_mark_rejects_unknown_resume(client, staff_user, staff_user_2, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    _, rows = await _done_run(client, h, acc)
    row = rows["https://jobs.example.com/remote"]
    r = await client.put(f"/job-applies/rows/{row['_id']}/applied", headers=h,
                         json={"accountId": str(acc.id), "resumeId": "nope", "applied": True})
    assert r.status_code == 400
    assert (await _mark(client, auth_header(staff_user_2), row)).status_code == 404


async def test_mark_all_top_applied_and_undo(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id, rows = await _done_run(client, h, acc)
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["toApply"] == 2  # remote + fetched greenhouse job; the hybrid one is excluded

    r = await client.post(f"/job-applies/runs/{run_id}/mark-top-applied", headers=h, json={"all": True})
    body = r.json()
    assert r.status_code == 200 and body["marked"] == 2 and len(body["marks"]) == 2
    assert (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["toApply"] == 0

    r = await client.post(f"/job-applies/runs/{run_id}/unmark-applied", headers=h, json={"marks": body["marks"]})
    assert r.status_code == 200 and r.json()["unmarked"] == 2
    assert (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["toApply"] == 2

    other = await client.post(f"/job-applies/runs/{run_id}/mark-top-applied", headers=h, json={"all": True, "accountId": "000000000000000000000000"})
    assert other.json()["marked"] == 0


async def test_mark_all_skips_jobs_applied_in_earlier_runs(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    _, rows = await _done_run(client, h, acc)
    await _mark(client, h, rows["https://jobs.example.com/remote"])
    run2, _ = await _done_run(client, h, acc)
    assert (await client.get(f"/job-applies/runs/{run2}", headers=h)).json()["toApply"] == 1
    r = await client.post(f"/job-applies/runs/{run2}/mark-top-applied", headers=h, json={"all": True})
    assert r.json()["marked"] == 1



async def _two_profile_run(client, h, owner):
    strong = await make_account(owner, resumes=(("strong.md", STRONG),), name="Strong")
    weak = await make_account(owner, resumes=(("weak.md", WEAK),), name="Weak")
    sel = [{"accountId": str(a.id), "resumeIds": []} for a in (weak, strong)]
    r = await client.post("/job-applies/runs", headers=h,
                          files={"file": ("jobs.xlsx", SHEET, "application/octet-stream")},
                          data={"selection": json.dumps(sel), "threshold": "60"})
    run_id = r.json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows?view=all", headers=h)).json()["rows"]
    return run_id, {x["url"]: x for x in rows}, strong, weak


async def test_tailor_defaults_to_best_profile(client, staff_user, auth_header):
    from app.models import ResumeApplication

    h = auth_header(staff_user)
    run_id, rows, strong, _ = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    r = await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={})
    assert r.status_code == 200, r.text
    t = r.json()["tailored"]
    assert t["accountId"] == str(strong.id) and t["status"] == "queued"
    job = await ResumeApplication.get(t["jobId"])
    assert job.source == "job_applies" and job.countsAsBid is False and str(job.jobApplyRowId) == row["_id"]
    assert job.jobUrl == row["url"] and "Python" in job.jobDescription


async def test_tailor_rejects_profile_outside_run(client, staff_user, auth_header):
    h = auth_header(staff_user)
    _, rows, _, _ = await _two_profile_run(client, h, staff_user)
    outsider = await make_account(staff_user, name="Other")
    row = rows["https://jobs.example.com/remote"]
    r = await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={"accountId": str(outsider.id)})
    assert r.status_code == 400


async def test_retailor_replaces_queued_job(client, staff_user, auth_header):
    from app.models import ResumeApplication
    from app.models.job_apply import JobApplyRow

    h = auth_header(staff_user)
    _, rows, strong, weak = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    first = (await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={})).json()["tailored"]
    second = (await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={"accountId": str(weak.id)})).json()["tailored"]
    assert await ResumeApplication.get(first["jobId"]) is None  # still queued and unused -> replaced
    fresh = await JobApplyRow.get(PydanticObjectId(row["_id"]))
    assert str(fresh.tailoredJobId) == second["jobId"] and fresh.tailoredAccountId == str(weak.id)


async def test_tailor_all_respects_cap(client, staff_user, auth_header, monkeypatch):
    from app import resume_queue

    monkeypatch.setattr(resume_queue, "_daily_cap", lambda: 1)
    h = auth_header(staff_user)
    run_id, _, _, _ = await _two_profile_run(client, h, staff_user)
    r = await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h, json={})
    assert r.status_code == 200 and r.json() == {"queued": 1, "skippedCap": 1}
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["tailoring"] == {"queued": 1, "inProgress": 0, "ready": 0, "failed": 0}


async def test_rows_show_tailored_status_and_other_resumes(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id, rows, strong, weak = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={})
    listed = {x["url"]: x for x in (await client.get(f"/job-applies/runs/{run_id}/rows?view=all", headers=h)).json()["rows"]}
    remote = listed["https://jobs.example.com/remote"]
    assert remote["tailored"]["status"] == "queued" and remote["tailored"]["hasPdf"] is False
    assert [o["filename"] for o in remote["otherResumes"]] == ["weak.md"]  # scored but below the threshold
    assert listed["https://jobs.example.com/hybrid"]["tailored"] is None
