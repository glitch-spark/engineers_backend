"""Job Applies API lifecycle against a real test database. The worker is driven directly with fake
fetch/extract functions, so no network or LLM is used."""
import io
import json
from datetime import date, datetime, timedelta

import pytest
from beanie import PydanticObjectId
from openpyxl import Workbook

from app.job_apply import worker
from app.job_apply.fetch import FetchError, FetchResult, JobClosed
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


async def create_screen_run(client, headers, *, data=SHEET, max_age=30):
    """A run created without profiles: it screens the jobs and waits for review."""
    return await client.post(
        "/job-applies/runs",
        headers=headers,
        files={"file": ("jobs.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"maxAgeDays": str(max_age)},
    )


async def screened_run(client, headers) -> str:
    r = await create_screen_run(client, headers)
    assert r.status_code == 202, r.text
    run_id = r.json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    return run_id


async def test_screen_phase_pauses(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["status"] == "screened" and run["phase"] == "screen" and run["screenedAt"]
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    by_url = {x["url"]: x for x in rows}
    assert all(x["status"] in ("extracted", "excluded") for x in rows)
    detail = (await client.get(f"/job-applies/rows/{by_url['https://jobs.example.com/hybrid']['_id']}", headers=h)).json()
    assert detail["screen"] == "onsite" and detail["groupKey"] == "US"
    remote = (await client.get(f"/job-applies/rows/{by_url['https://jobs.example.com/remote']['_id']}", headers=h)).json()
    assert remote["screen"] in ("valid", "check") and remote["groupKey"] == "US" and not remote.get("scoredAt")


async def test_closed_fetch_is_bucketed(client, staff_user, auth_header, monkeypatch):
    async def closed(url, *, client, browser=None):
        raise JobClosed("job closed (HTTP 404)")

    monkeypatch.setattr(worker, "FETCH", closed)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    gh = next(x for x in rows if "greenhouse" in x["url"])
    detail = (await client.get(f"/job-applies/rows/{gh['_id']}", headers=h)).json()
    assert detail["status"] == "fetch_failed" and detail["screen"] == "closed"


async def test_create_with_selection_runs_through(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    r = await create_run(client, h, acc)
    run_id = r.json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["status"] == "done" and run["autoStart"] is True and run["phase"] == "score"
    assert run["assignments"] == {"*": [str(acc.id)]}


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
    assert rows and all(r["appliedResumes"] for r in rows)  # the worker's writes kept the marks


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


async def test_tailor_forwards_requested_models(client, staff_user, auth_header, anthropic_key):
    from app.models import ResumeApplication

    h = auth_header(staff_user)
    _, rows, _, _ = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    r = await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={
        "resumeModel": "anthropic:claude-opus-5-5", "coverLetter": True,
        "coverLetterModel": "anthropic:claude-fable-5-1"})
    assert r.status_code == 200, r.text
    job = await ResumeApplication.get(r.json()["tailored"]["jobId"])
    assert job.resumeModelId == "openai:gpt-4o-mini"  # Job Applies always tailors with GPT-4o mini
    assert job.coverLetterModelId == "anthropic:claude-fable-5-1"


async def test_tailor_unknown_model_is_400(client, staff_user, auth_header, anthropic_key):
    h = auth_header(staff_user)
    _, rows, _, _ = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    r = await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={"coverLetterModel": "anthropic:nope"})
    assert r.status_code == 400 and "anthropic:nope" in r.json()["detail"]


async def test_retailor_replaces_queued_job(client, staff_user, auth_header):
    from app.models import ResumeApplication
    from app.models.job_apply import JobApplyRow

    h = auth_header(staff_user)
    _, rows, strong, weak = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    first = (await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={})).json()["tailored"]
    again = (await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={})).json()["tailored"]
    weak_job = (await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={"accountId": str(weak.id)})).json()["tailored"]
    assert await ResumeApplication.get(first["jobId"]) is None  # same profile, still queued and unused -> replaced
    fresh = await JobApplyRow.get(PydanticObjectId(row["_id"]))
    assert {k: str(v) for k, v in fresh.tailored.items()} == {str(strong.id): again["jobId"], str(weak.id): weak_job["jobId"]}


async def test_tailor_all_respects_cap(client, staff_user, auth_header, monkeypatch):
    from app import resume_queue

    monkeypatch.setattr(resume_queue, "_daily_cap", lambda: 1)
    h = auth_header(staff_user)
    run_id, _, _, _ = await _two_profile_run(client, h, staff_user)
    r = await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h, json={})
    assert r.status_code == 200 and r.json() == {"queued": 1, "skippedCap": 3, "skipped": 0}
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["tailoring"] == {"queued": 1, "inProgress": 0, "ready": 0, "failed": 0}


async def test_rows_show_tailored_status_and_other_resumes(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id, rows, strong, weak = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={})
    listed = {x["url"]: x for x in (await client.get(f"/job-applies/runs/{run_id}/rows?view=all", headers=h)).json()["rows"]}
    remote = listed["https://jobs.example.com/remote"]
    (t,) = remote["tailored"]
    assert t["status"] == "queued" and t["hasPdf"] is False and t["accountId"] == str(strong.id)
    assert set(remote["openProfiles"]) == {str(strong.id), str(weak.id)}
    assert listed["https://jobs.example.com/hybrid"]["tailored"] == []


async def _tailored_ready(client, h, row):
    from app.models import ResumeApplication

    t = (await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={})).json()["tailored"]
    await ResumeApplication.get_motor_collection().update_one(
        {"_id": PydanticObjectId(t["jobId"])}, {"$set": {"status": "completed", "step": "done", "pdfData": "JVBERg=="}}
    )
    return t


async def test_mark_tailored_requires_completed_pdf(client, staff_user, auth_header):
    h = auth_header(staff_user)
    _, rows, strong, _ = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    t = (await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={})).json()["tailored"]
    url = f"/job-applies/rows/{row['_id']}/applied"
    body = {"accountId": t["accountId"], "tailoredJobId": t["jobId"], "applied": True}
    r = await client.put(url, headers=h, json=body)
    assert r.status_code == 400 and "isn't ready" in r.json()["error"]
    both = await client.put(url, headers=h, json={**body, "resumeId": row["suggestions"][0]["resumeId"]})
    assert both.status_code == 400
    neither = await client.put(url, headers=h, json={"accountId": t["accountId"], "applied": True})
    assert neither.status_code == 400


async def test_mark_tailored_counts_bid_and_unmark_removes_it(client, staff_user, auth_header):
    from app.models import ResumeApplication
    from app.models.job_apply import JobApplicationLog

    h = auth_header(staff_user)
    _, rows, _, _ = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    t = await _tailored_ready(client, h, row)
    url = f"/job-applies/rows/{row['_id']}/applied"
    r = await client.put(url, headers=h, json={"accountId": t["accountId"], "tailoredJobId": t["jobId"], "applied": True})
    assert r.status_code == 200 and r.json()["appliedResumes"][0]["tailoredJobId"] == t["jobId"]
    assert (await ResumeApplication.get(t["jobId"])).countsAsBid is True
    (log,) = await JobApplicationLog.find_all().to_list()
    assert log.fileKey == f"tailored:{t['jobId']}" and log.tailoredJobId == t["jobId"] and log.resumeId is None

    await client.put(url, headers=h, json={"accountId": t["accountId"], "tailoredJobId": t["jobId"], "applied": False})
    assert (await ResumeApplication.get(t["jobId"])).countsAsBid is False
    assert await JobApplicationLog.find_all().count() == 0


async def test_mark_other_uploaded_resume(client, staff_user, auth_header):
    h = auth_header(staff_user)
    _, rows, _, weak = await _two_profile_run(client, h, staff_user)
    row = rows["https://jobs.example.com/remote"]
    detail = (await client.get(f"/job-applies/rows/{row['_id']}", headers=h)).json()
    shown = {x["resumeId"] for x in row["suggestions"]}
    other = next(x for x in detail["scores"] if x["resumeId"] not in shown)  # scored, below the threshold
    r = await client.put(f"/job-applies/rows/{row['_id']}/applied", headers=h,
                         json={"accountId": other["accountId"], "resumeId": other["resumeId"], "applied": True})
    assert r.status_code == 200 and r.json()["appliedResumes"][0]["resumeId"] == other["resumeId"]


async def test_mark_top_prefers_ready_tailored(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id, rows, _, _ = await _two_profile_run(client, h, staff_user)
    t = await _tailored_ready(client, h, rows["https://jobs.example.com/remote"])
    marks = (await client.post(f"/job-applies/runs/{run_id}/mark-top-applied", headers=h, json={"all": True})).json()["marks"]
    by_row = {m["rowId"]: m for m in marks}
    assert by_row[rows["https://jobs.example.com/remote"]["_id"]]["tailoredJobId"] == t["jobId"]
    gh = by_row[rows["https://boards.greenhouse.io/acme/jobs/9"]["_id"]]
    assert gh.get("resumeId") and not gh.get("tailoredJobId")
    undo = await client.post(f"/job-applies/runs/{run_id}/unmark-applied", headers=h, json={"marks": marks})
    assert undo.json()["unmarked"] == 2


async def test_log_backfill_filekey(client, staff_user, auth_header):
    from app.db import _migrate_log_filekey
    from app.models.job_apply import JobApplicationLog

    coll = JobApplicationLog.get_motor_collection()
    await coll.insert_one({"userId": staff_user.id, "url": "https://jobs.example.com/remote", "accountId": "a",
                           "resumeId": "r1", "profileName": "Old", "filename": "old.pdf",
                           "appliedAt": datetime.utcnow(), "createdAt": datetime.utcnow(), "updatedAt": datetime.utcnow()})
    await _migrate_log_filekey(coll.database)
    doc = await coll.find_one({"resumeId": "r1"})
    assert doc["fileKey"] == "r1"
    h = auth_header(staff_user)
    _, rows = await _done_run(client, h, await make_account(staff_user))
    assert rows["https://jobs.example.com/remote"]["previouslyApplied"][0]["profileName"] == "Old"


async def test_concurrent_tailor_keeps_one_job(client, staff_user, auth_header):
    """Two requests that both read the row before either writes must not leave an orphaned queued job."""
    from app.models import ResumeApplication, UserExtra
    from app.models.job_apply import JobApplyRow, JobApplyRun
    from app.routers.job_applies import _tailor_row

    h = auth_header(staff_user)
    run_id, rows, strong, _ = await _two_profile_run(client, h, staff_user)
    row_id = PydanticObjectId(rows["https://jobs.example.com/remote"]["_id"])
    stale_a, stale_b = await JobApplyRow.get(row_id), await JobApplyRow.get(row_id)
    run = await JobApplyRun.get(PydanticObjectId(run_id))
    owner = await UserExtra.get(staff_user.id)
    await _tailor_row(stale_a, run, owner, None, False)
    await _tailor_row(stale_b, run, owner, None, False)
    jobs = await ResumeApplication.find({"jobApplyRowId": row_id}).to_list()
    fresh = await JobApplyRow.get(row_id)
    assert len(jobs) == 1 and fresh.tailored[str(strong.id)] == jobs[0].id


async def test_tailor_all_skips_rows_it_cannot_tailor(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id, _, strong, _ = await _two_profile_run(client, h, staff_user)
    strong.styleTemplate = None  # the best profile has no HTML template
    await strong.save()
    r = await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == 2 and r.json()["skipped"] == 2  # weak has a template; strong doesn't


async def test_deleting_a_run_drops_its_queued_tailored_jobs(client, staff_user, auth_header):
    from app.models import ResumeApplication

    h = auth_header(staff_user)
    run_id, rows, _, _ = await _two_profile_run(client, h, staff_user)
    await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h, json={})
    ready = await _tailored_ready(client, h, rows["https://boards.greenhouse.io/acme/jobs/9"])  # completed one stays
    assert await ResumeApplication.find({"source": "job_applies", "status": "queued"}).count() == 3
    assert (await client.delete(f"/job-applies/runs/{run_id}", headers=h)).status_code == 200
    assert await ResumeApplication.find({"source": "job_applies", "status": "queued"}).count() == 0
    assert await ResumeApplication.get(ready["jobId"]) is not None


async def _tailor_only_account(owner, country="US", template=True):
    acc = Account(name="Tailor Only", country=country, region="US" if country == "US" else "Latam", createdBy=owner.id,
                  resumes=[], styleTemplate="<html><body><h1>T</h1></body></html>" if template else None)
    await acc.insert()
    return acc


async def _run_with(client, h, accounts, data=SHEET):
    sel = [{"accountId": str(a.id), "resumeIds": []} for a in accounts]
    r = await client.post("/job-applies/runs", headers=h, files={"file": ("jobs.xlsx", data, "application/octet-stream")},
                          data={"selection": json.dumps(sel), "threshold": "60"})
    return r


async def test_tailor_only_profile_run(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await _tailor_only_account(staff_user)
    r = await _run_with(client, h, [acc])
    assert r.status_code == 202, r.text
    run_id = r.json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows?view=suggested", headers=h)).json()["rows"]
    by_url = {x["url"]: x for x in rows}
    assert set(by_url) == {"https://jobs.example.com/remote", "https://boards.greenhouse.io/acme/jobs/9"}  # hybrid excluded
    assert by_url["https://jobs.example.com/remote"]["tailorOnly"] == [str(acc.id)]
    assert by_url["https://jobs.example.com/remote"]["suggestions"] == []
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["toApply"] == 2 and run["suggested"] == 2

    r = await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h, json={})
    assert r.json()["queued"] == 2
    (t,) = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"][0]["tailored"]
    assert t["accountId"] == str(acc.id)


async def test_tailor_only_profile_respects_location(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await _tailor_only_account(staff_user, country="BR")
    run_id = (await _run_with(client, h, [acc])).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows?view=all", headers=h)).json()["rows"]
    assert all(x["tailorOnly"] == [] for x in rows)
    assert (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["toApply"] == 0


async def test_profile_without_resumes_or_template_rejected(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await _tailor_only_account(staff_user, template=False)
    r = await _run_with(client, h, [acc])
    assert r.status_code == 400 and "no resumes and no HTML template" in r.json()["error"]


async def test_sheet_preview(client, staff_user, auth_header, monkeypatch):
    from app.job_apply import gsheet
    from app.job_apply.sheet import parse_sheet

    h = auth_header(staff_user)
    r = await client.post("/job-applies/sheet-preview", headers=h, files={"file": ("jobs.xlsx", SHEET, "application/octet-stream")})
    assert r.status_code == 200 and r.json() == {
        "title": "jobs.xlsx", "total": 3, "withDescription": 2, "urlOnly": 1,
        "links": 3, "duplicates": 0, "cleaned": 0, "blocked": 0,
    }
    bad = await client.post("/job-applies/sheet-preview", headers=h, files={"file": ("x.xlsx", b"nope", "application/octet-stream")})
    assert bad.status_code == 400

    async def fake(url, *, client):
        return "Jobs Oct - Sheet1", parse_sheet("s.xlsx", SHEET)

    monkeypatch.setattr(gsheet, "download_gsheet", fake)
    r = await client.post("/job-applies/sheet-preview", headers=h, data={"sheetUrl": GSHEET})
    assert r.json()["title"] == "Jobs Oct - Sheet1" and r.json()["total"] == 3


async def test_tailor_only_jobs_are_not_listed_as_excluded(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await _tailor_only_account(staff_user)
    run_id = (await _run_with(client, h, [acc])).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    excluded = (await client.get(f"/job-applies/runs/{run_id}/rows?view=excluded", headers=h)).json()["rows"]
    assert [x["url"] for x in excluded] == ["https://jobs.example.com/hybrid"]
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["excludedCount"] == 1


async def test_excluded_count_includes_profiles_blocked_by_location(client, staff_user, auth_header):
    h = auth_header(staff_user)
    acc = await make_account(staff_user, country="BR")  # every US-only job is closed to this profile
    run_id = (await create_run(client, h, acc)).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    excluded = (await client.get(f"/job-applies/runs/{run_id}/rows?view=excluded", headers=h)).json()["rows"]
    assert run["excludedCount"] == len(excluded) == 3



async def test_each_profile_gets_its_own_tailored_resume(client, staff_user, auth_header):
    """Tharun (no resumes, tailor only) and a profile with a resume can both be tailored for one job."""
    h = auth_header(staff_user)
    tharun = await _tailor_only_account(staff_user)
    eric = await make_account(staff_user, resumes=(("eric.md", STRONG),), name="Eric")
    run_id = (await _run_with(client, h, [tharun, eric])).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    row = next(x for x in (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
               if x["url"] == "https://jobs.example.com/remote")
    assert set(row["openProfiles"]) == {str(tharun.id), str(eric.id)}
    for acc in (tharun, eric):
        r = await client.post(f"/job-applies/rows/{row['_id']}/tailor", headers=h, json={"accountId": str(acc.id)})
        assert r.status_code == 200 and r.json()["tailored"]["accountId"] == str(acc.id)
    again = next(x for x in (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"] if x["_id"] == row["_id"])
    assert {t["accountId"] for t in again["tailored"]} == {str(tharun.id), str(eric.id)}


async def test_tailor_all_for_chosen_profiles_with_dry_run(client, staff_user, auth_header):
    h = auth_header(staff_user)
    tharun = await _tailor_only_account(staff_user)
    eric = await make_account(staff_user, resumes=(("eric.md", STRONG),), name="Eric")
    run_id = (await _run_with(client, h, [tharun, eric])).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    preview = (await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h,
                                 json={"accountIds": [str(tharun.id)], "dryRun": True})).json()
    assert preview == {"queued": 2, "skippedCap": 0, "skipped": 0}
    from app.models import ResumeApplication
    assert await ResumeApplication.find({"source": "job_applies"}).count() == 0  # dry run queues nothing
    r = await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h, json={"accountIds": [str(tharun.id)]})
    assert r.json()["queued"] == 2
    jobs = await ResumeApplication.find({"source": "job_applies"}).to_list()
    assert {str(j.accountId) for j in jobs} == {str(tharun.id)}


async def test_legacy_single_tailored_link_is_migrated(client, staff_user, auth_header):
    from app.db import _migrate_tailored_map
    from app.models.job_apply import JobApplyRow

    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id, rows = await _done_run(client, h, acc)
    row_id = PydanticObjectId(rows["https://jobs.example.com/remote"]["_id"])
    legacy = PydanticObjectId()
    coll = JobApplyRow.get_motor_collection()
    await coll.update_one({"_id": row_id}, {"$set": {"tailoredJobId": legacy, "tailoredAccountId": str(acc.id)}})
    await _migrate_tailored_map(coll.database)
    doc = await coll.find_one({"_id": row_id})
    assert doc["tailored"] == {str(acc.id): legacy} and "tailoredJobId" not in doc


async def _two_people_run(client, h, owner):
    """Tharun (tailor-only) + Eric (one strong resume): remote & greenhouse jobs are open to both."""
    tharun = await _tailor_only_account(owner)
    eric = await make_account(owner, resumes=(("eric.md", STRONG),), name="Eric")
    run_id = (await _run_with(client, h, [tharun, eric])).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    rows = {x["url"]: x for x in (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]}
    return run_id, rows, tharun, eric


async def test_applications_count_per_profile(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric = await _two_people_run(client, h, staff_user)
    apps = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["applications"]
    # 2 jobs × (Tharun needs a tailored resume, Eric ready with his uploaded one)
    assert apps["jobs"] == 2 and apps["toGo"] == 4 and apps["ready"] == 2 and apps["needsResume"] == 2
    assert apps["tailoring"] == 0 and apps["applied"] == 0
    assert {p["accountId"]: p["toGo"] for p in apps["byProfile"]} == {str(tharun.id): 2, str(eric.id): 2}
    assert {p["accountId"]: p["ready"] for p in apps["byProfile"]} == {str(tharun.id): 0, str(eric.id): 2}


async def test_job_stays_to_apply_until_every_profile_applied(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric = await _two_people_run(client, h, staff_user)
    remote = rows["https://jobs.example.com/remote"]
    s = next(x for x in remote["suggestions"] if x["accountId"] == str(eric.id))
    await client.put(f"/job-applies/rows/{remote['_id']}/applied", headers=h,
                     json={"accountId": str(eric.id), "resumeId": s["resumeId"], "applied": True})
    to_apply = {x["url"] for x in (await client.get(f"/job-applies/runs/{run_id}/rows?view=suggested&applied=no", headers=h)).json()["rows"]}
    assert "https://jobs.example.com/remote" in to_apply  # Tharun still has to apply
    applied = (await client.get(f"/job-applies/runs/{run_id}/rows?view=suggested&applied=yes", headers=h)).json()["rows"]
    assert applied == []
    apps = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["applications"]
    assert apps["applied"] == 1 and apps["toGo"] == 3


async def test_mark_all_marks_every_ready_application(client, staff_user, auth_header):
    from app.models import ResumeApplication

    h = auth_header(staff_user)
    run_id, rows, tharun, eric = await _two_people_run(client, h, staff_user)
    remote = rows["https://jobs.example.com/remote"]
    t = (await client.post(f"/job-applies/rows/{remote['_id']}/tailor", headers=h, json={"accountId": str(tharun.id)})).json()["tailored"]
    await ResumeApplication.get_motor_collection().update_one(
        {"_id": PydanticObjectId(t["jobId"])}, {"$set": {"status": "completed", "step": "done", "pdfData": "JVBERg=="}}
    )
    res = (await client.post(f"/job-applies/runs/{run_id}/mark-top-applied", headers=h, json={"all": True})).json()
    # Eric on both jobs (uploaded) + Tharun on the remote job (tailored); Tharun on greenhouse has nothing yet.
    assert res["marked"] == 3
    assert {(m["accountId"], bool(m.get("tailoredJobId"))) for m in res["marks"]} == {
        (str(eric.id), False), (str(tharun.id), True)}
    apps = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["applications"]
    assert apps["applied"] == 3 and apps["toGo"] == 1 and apps["jobs"] == 1


# ---------- screening: report, start, include anyway, max age, retry ----------


async def rows_by_url(client, h, run_id, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows?{q}", headers=h)).json()["rows"]
    return {x["url"]: x for x in rows}


async def start(client, h, run_id, assignments, accounts, threshold=60):
    body = {
        "assignments": {k: [str(a.id) for a in v] for k, v in assignments.items()},
        "selection": [{"accountId": str(a.id), "resumeIds": []} for a in accounts],
        "threshold": threshold,
    }
    return await client.post(f"/job-applies/runs/{run_id}/start", headers=h, json=body)


async def test_screening_report(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    run_id = await screened_run(client, h)
    rep = (await client.get(f"/job-applies/runs/{run_id}/screening", headers=h)).json()
    assert rep["total"] == 3 and rep["worth"] == 2 and rep["buckets"]["onsite"] == 1 and rep["maxAgeDays"] == 30
    assert [g["key"] for g in rep["groups"]] == ["US", "UKEU", "LATAM"] and rep["others"] == []
    assert rep["groups"][0]["jobs"] == 2 and rep["groups"][0]["fits"] == [str(us.id)]
    assert rep["groups"][1]["jobs"] == 0 and rep["groups"][1]["fits"] == []
    assert rep["profiles"][0]["id"] == str(us.id) and rep["profiles"][0]["resumes"] == 2


async def test_start_scores_assigned_only(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user, name="US")
    gb = await make_account(staff_user, country="GB", name="UK")
    run_id = await screened_run(client, h)
    r = await start(client, h, run_id, {"US": [us]}, [us, gb])
    assert r.status_code == 200, r.text
    await worker.run_worker(PydanticObjectId(run_id))
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["status"] == "done" and run["phase"] == "score"
    rows = await rows_by_url(client, h, run_id)
    remote = rows["https://jobs.example.com/remote"]
    assert remote["status"] == "scored"
    detail = (await client.get(f"/job-applies/rows/{remote['_id']}", headers=h)).json()
    assert {s["accountId"] for s in detail["scores"]} == {str(us.id)}
    assert rows["https://jobs.example.com/hybrid"]["status"] == "excluded"


async def test_start_unassigned_group(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    run_id = await screened_run(client, h)
    r = await start(client, h, run_id, {"GB": [us]}, [us])
    assert r.status_code == 400 and "at least one profile" in r.json()["error"]


async def test_start_twice_409(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    run_id = await screened_run(client, h)
    assert (await start(client, h, run_id, {"US": [us]}, [us])).status_code == 200
    r = await start(client, h, run_id, {"US": [us]}, [us])
    assert r.status_code == 409 and r.json()["error"] == "The run is still scoring."


async def test_start_requires_screened(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    r = await create_screen_run(client, h)
    assert (await start(client, h, r.json()["runId"], {"US": [us]}, [us])).status_code == 409


async def test_start_archived_profile_404(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    run_id = await screened_run(client, h)
    us.archived = True
    await us.save()
    assert (await start(client, h, run_id, {"US": [us]}, [us])).status_code == 404
    rep = (await client.get(f"/job-applies/runs/{run_id}/screening", headers=h)).json()
    assert rep["profiles"] == []


async def test_max_age_patch_rescreens(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    r = await client.patch(f"/job-applies/runs/{run_id}", headers=h, json={"maxAgeDays": 1})
    assert r.status_code == 200, r.text
    rep = (await client.get(f"/job-applies/runs/{run_id}/screening", headers=h)).json()
    assert rep["maxAgeDays"] == 1 and rep["worth"] == 0 and rep["buckets"]["too_old"] == 2
    await client.patch(f"/job-applies/runs/{run_id}", headers=h, json={"maxAgeDays": 30})
    rep = (await client.get(f"/job-applies/runs/{run_id}/screening", headers=h)).json()
    assert rep["worth"] == 2 and rep["buckets"]["too_old"] == 0


async def test_max_age_patch_409_when_not_screened(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    run_id = await screened_run(client, h)
    await start(client, h, run_id, {"US": [us]}, [us])
    r = await client.patch(f"/job-applies/runs/{run_id}", headers=h, json={"maxAgeDays": 5})
    assert r.status_code == 409 and r.json()["error"] == "Max age can only change before scoring starts."
    assert (await client.patch(f"/job-applies/runs/{run_id}", headers=h, json={"threshold": 50})).status_code == 200


async def test_approve_moves_a_job_to_worth_applying(client, staff_user, auth_header, monkeypatch):
    monkeypatch.setattr(worker, "FETCH", failing_fetch)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    rows = await rows_by_url(client, h, run_id)
    remote, hybrid, gh = (rows[f"https://{u}"] for u in (
        "jobs.example.com/remote", "jobs.example.com/hybrid", "boards.greenhouse.io/acme/jobs/9"))
    r = await client.put(f"/job-applies/rows/{hybrid['_id']}/include", headers=h, json={"include": True})
    assert r.status_code == 200 and r.json()["screen"] == "valid" and r.json()["forceInclude"] is True
    rep = (await client.get(f"/job-applies/runs/{run_id}/screening", headers=h)).json()
    assert rep["buckets"]["onsite"] == 0 and rep["buckets"]["valid"] == 2 and rep["check"] == 0
    # nothing to approve: already worth applying, or never read (no description to score or tailor from)
    r = await client.put(f"/job-applies/rows/{remote['_id']}/include", headers=h, json={"include": True})
    assert r.status_code == 400 and "already worth applying" in r.json()["error"]
    r = await client.put(f"/job-applies/rows/{gh['_id']}/include", headers=h, json={"include": True})
    assert r.status_code == 400 and "job description" in r.json()["error"]
    r = await client.put(f"/job-applies/rows/{hybrid['_id']}/include", headers=h, json={"include": False})
    assert r.json()["screen"] == "onsite" and r.json()["forceInclude"] is False


async def _no_location_extract(jd, *, user, run_date, llm=None):
    ex, source = await fake_extract(jd, user=user, run_date=run_date)
    ex.allowedLocations = []  # a job that doesn't say where it's open to is Needs a check
    return ex, source


async def test_approve_a_needs_a_check_job(client, staff_user, auth_header, monkeypatch):
    monkeypatch.setattr(worker, "EXTRACT", _no_location_extract)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    rep = (await client.get(f"/job-applies/runs/{run_id}/screening", headers=h)).json()
    assert rep["check"] == 2 and rep["worth"] == 0 and rep["buckets"]["valid"] == 0
    row = next(iter((await rows_by_url(client, h, run_id, screen="check")).values()))
    r = await client.put(f"/job-applies/rows/{row['_id']}/include", headers=h, json={"include": True})
    assert r.status_code == 200 and r.json()["screen"] == "valid"
    rep = (await client.get(f"/job-applies/runs/{run_id}/screening", headers=h)).json()
    assert rep["check"] == 1 and rep["worth"] == 1 and rep["buckets"]["valid"] == 1 and rep["buckets"]["check"] == 1
    r = await client.put(f"/job-applies/rows/{row['_id']}/include", headers=h, json={"include": False})
    assert r.json()["screen"] == "check"


async def test_needs_a_check_job_is_scored_only_once_approved(client, staff_user, auth_header, monkeypatch):
    monkeypatch.setattr(worker, "EXTRACT", _no_location_extract)  # remote + greenhouse: Needs a check
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    run_id = await screened_run(client, h)
    rows = await rows_by_url(client, h, run_id)
    remote, gh = rows["https://jobs.example.com/remote"], rows["https://boards.greenhouse.io/acme/jobs/9"]
    assert remote["screen"] == gh["screen"] == "check"
    # nothing approved yet: no job is worth applying to, so there is nothing to score
    assert (await start(client, h, run_id, {"none": [us]}, [us])).status_code == 400
    assert (await client.put(f"/job-applies/rows/{remote['_id']}/include", headers=h, json={"include": True})).status_code == 200
    assert (await start(client, h, run_id, {"none": [us]}, [us])).status_code == 200
    await worker.run_worker(PydanticObjectId(run_id))
    rows = await rows_by_url(client, h, run_id)
    assert rows["https://jobs.example.com/remote"]["status"] == "scored"
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    assert gh["status"] == "extracted" and not gh.get("scoredAt") and gh["screen"] == "check"
    assert "https://boards.greenhouse.io/acme/jobs/9" not in await rows_by_url(client, h, run_id, view="suggested")


async def test_approved_other_location_job_is_scored_with_the_not_stated_picks(client, staff_user, auth_header, monkeypatch):
    async def canada_only(jd, *, user, run_date, llm=None):
        ex, source = await fake_extract(jd, user=user, run_date=run_date)
        ex.allowedLocations = [Location(kind="country", value="CA")]
        return ex, source

    monkeypatch.setattr(worker, "EXTRACT", canada_only)
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    run_id = await screened_run(client, h)
    remote = (await rows_by_url(client, h, run_id))["https://jobs.example.com/remote"]
    assert remote["screen"] == "other_location"
    assert (await client.put(f"/job-applies/rows/{remote['_id']}/include", headers=h, json={"include": True})).status_code == 200
    rep = (await client.get(f"/job-applies/runs/{run_id}/screening", headers=h)).json()
    assert [g["key"] for g in rep["groups"]] == ["US", "UKEU", "LATAM", "none"] and rep["groups"][-1]["jobs"] == 1
    assert rep["groups"][-1]["fits"] == [str(us.id)]
    r = await start(client, h, run_id, {"none": [us]}, [us])
    assert r.status_code == 200, r.text
    await worker.run_worker(PydanticObjectId(run_id))
    assert (await rows_by_url(client, h, run_id))["https://jobs.example.com/remote"]["status"] == "scored"


async def test_rescore_sends_an_approved_job_back_by_whether_it_failed_a_check(client, staff_user, auth_header, monkeypatch):
    from app.models.job_apply import JobApplyRow

    monkeypatch.setattr(worker, "EXTRACT", _no_location_extract)  # remote + greenhouse: Needs a check; hybrid: on-site
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    run_id = await screened_run(client, h)
    rows = await rows_by_url(client, h, run_id)
    hybrid, remote = rows["https://jobs.example.com/hybrid"], rows["https://jobs.example.com/remote"]
    for row in (hybrid, remote):
        assert (await client.put(f"/job-applies/rows/{row['_id']}/include", headers=h, json={"include": True})).status_code == 200
    assert (await start(client, h, run_id, {"none": [us]}, [us])).status_code == 200
    await worker.run_worker(PydanticObjectId(run_id))
    rows = await rows_by_url(client, h, run_id)
    assert rows["https://jobs.example.com/hybrid"]["status"] == rows["https://jobs.example.com/remote"]["status"] == "scored"
    assert (await start(client, h, run_id, {"none": [us]}, [us])).status_code == 200  # score again with the same picks
    # the on-site job failed a check, so until it is scored again it is excluded; the other only needed a check
    assert (await JobApplyRow.get(PydanticObjectId(hybrid["_id"]))).status == "excluded"
    assert (await JobApplyRow.get(PydanticObjectId(remote["_id"]))).status == "extracted"


async def test_tailor_all_for_chosen_rows_only(client, staff_user, auth_header):
    h = auth_header(staff_user)
    tharun = await _tailor_only_account(staff_user)
    run_id = (await _run_with(client, h, [tharun])).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    mine = [r["_id"] for r in rows if r.get("tailorOnly")][:1]
    assert mine, "the fixture should give at least one job this profile can be tailored for"
    body = {"accountIds": [str(tharun.id)], "rowIds": mine}
    assert (await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h, json={**body, "dryRun": True})).json() == {
        "queued": 1, "skippedCap": 0, "skipped": 0}
    r = await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h, json=body)
    assert r.json()["queued"] == 1
    from app.models import ResumeApplication
    assert await ResumeApplication.find({"source": "job_applies"}).count() == 1  # not one per suggested job
    # a row of another run is not reachable by id
    other = await screened_run(client, h)
    foreign = next(iter((await rows_by_url(client, h, other)).values()))["_id"]
    r = await client.post(f"/job-applies/runs/{run_id}/tailor-all", headers=h, json={**body, "rowIds": [foreign], "dryRun": True})
    assert r.json()["queued"] == 0


async def test_retry_from_screened_keeps_force_include(client, staff_user, auth_header, monkeypatch):
    monkeypatch.setattr(worker, "FETCH", failing_fetch)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    rows = await rows_by_url(client, h, run_id)
    await client.put(f"/job-applies/rows/{rows['https://jobs.example.com/hybrid']['_id']}/include", headers=h, json={"include": True})
    assert rows["https://boards.greenhouse.io/acme/jobs/9"]["screen"] == "not_fetched"
    monkeypatch.setattr(worker, "FETCH", fake_fetch)
    r = await client.post(f"/job-applies/runs/{run_id}/retry-failed", headers=h)
    assert r.status_code == 200 and r.json()["reset"] == 1
    await worker.run_worker(PydanticObjectId(run_id))
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["status"] == "screened"
    rows = await rows_by_url(client, h, run_id)
    assert rows["https://jobs.example.com/hybrid"]["screen"] == "valid"  # still approved
    assert rows["https://boards.greenhouse.io/acme/jobs/9"]["screen"] in ("valid", "check")


async def test_screen_view_projection():
    from app.routers.job_applies import _ScreenView

    assert set(_ScreenView.model_fields) == {"screen", "groupKey", "forceInclude"}


async def test_rows_screen_filter(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    rows = await rows_by_url(client, h, run_id, screen="onsite")
    assert list(rows) == ["https://jobs.example.com/hybrid"]


async def test_rows_list_includes_group(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    run_id = await screened_run(client, h)
    await start(client, h, run_id, {"US": [us]}, [us])
    await worker.run_worker(PydanticObjectId(run_id))
    rows = (await client.get(f"/job-applies/runs/{run_id}/rows", headers=h)).json()["rows"]
    assert rows and all(r["groupKey"] == "US" and r["screen"] for r in rows)


# ---------- final review fixes ----------


async def _strip_screening_fields(run_id):
    """Make a run and its rows look like they were created before screening existed."""
    from app.models.job_apply import JobApplyRow, JobApplyRun

    oid = PydanticObjectId(run_id)
    await JobApplyRun.get_motor_collection().update_one(
        {"_id": oid}, {"$unset": {"phase": "", "autoStart": "", "assignments": "", "screenedAt": ""}}
    )
    await JobApplyRow.get_motor_collection().update_many({"runId": oid}, {"$unset": {"screen": "", "groupKey": ""}})


async def test_legacy_runs_are_migrated(client, staff_user, auth_header):
    from app.db import _migrate_job_apply_screening
    from app.models.job_apply import JobApplyRun

    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    done_id = (await create_run(client, h, acc)).json()["runId"]
    await worker.run_worker(PydanticObjectId(done_id))
    await _strip_screening_fields(done_id)
    # a run that was mid-flight when the new code was deployed
    live_id = (await create_run(client, h, acc)).json()["runId"]
    await _strip_screening_fields(live_id)
    await JobApplyRun.get_motor_collection().update_one({"_id": PydanticObjectId(live_id)}, {"$set": {"status": "running"}})

    await _migrate_job_apply_screening(JobApplyRun.get_motor_collection().database)

    done = (await client.get(f"/job-applies/runs/{done_id}", headers=h)).json()
    assert done["phase"] == "score" and done["autoStart"] is True and done["assignments"] == {"*": [str(acc.id)]}
    rows = await rows_by_url(client, h, done_id)
    assert rows["https://jobs.example.com/remote"]["screen"] in ("valid", "check")
    assert rows["https://jobs.example.com/remote"]["groupKey"] == "US"
    assert rows["https://jobs.example.com/hybrid"]["screen"] == "onsite"

    await worker.run_worker(PydanticObjectId(live_id))
    live = (await client.get(f"/job-applies/runs/{live_id}", headers=h)).json()
    assert live["status"] == "done"
    assert (await rows_by_url(client, h, live_id))["https://jobs.example.com/remote"]["status"] == "scored"


async def test_retry_after_scoring_rescores(client, staff_user, auth_header, monkeypatch):
    monkeypatch.setattr(worker, "FETCH", failing_fetch)
    h = auth_header(staff_user)
    acc = await make_account(staff_user)
    run_id = (await create_run(client, h, acc)).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    monkeypatch.setattr(worker, "FETCH", fake_fetch)
    assert (await client.post(f"/job-applies/runs/{run_id}/retry-failed", headers=h)).json()["reset"] == 1
    await worker.run_worker(PydanticObjectId(run_id))
    gh = (await rows_by_url(client, h, run_id))["https://boards.greenhouse.io/acme/jobs/9"]
    assert gh["status"] == "scored" and gh["screen"] in ("valid", "check")


async def test_retry_skips_closed(client, staff_user, auth_header, monkeypatch):
    async def closed(url, *, client, browser=None):
        raise JobClosed("job closed (HTTP 404)")

    monkeypatch.setattr(worker, "FETCH", closed)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["counts"]["failed"] == 1 and run["retryable"] == 0
    assert (await client.post(f"/job-applies/runs/{run_id}/retry-failed", headers=h)).json()["reset"] == 0
    gh = (await rows_by_url(client, h, run_id))["https://boards.greenhouse.io/acme/jobs/9"]
    assert gh["status"] == "fetch_failed" and gh["screen"] == "closed"


async def test_max_age_patch_writes_only_changed_rows(client, staff_user, auth_header):
    from app.models.job_apply import JobApplyRow

    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    coll = JobApplyRow.get_motor_collection()
    before = {d["_id"]: d["updatedAt"] async for d in coll.find({"runId": PydanticObjectId(run_id)}, {"updatedAt": 1})}
    assert (await client.patch(f"/job-applies/runs/{run_id}", headers=h, json={"maxAgeDays": 29})).status_code == 200
    after = {d["_id"]: d["updatedAt"] async for d in coll.find({"runId": PydanticObjectId(run_id)}, {"updatedAt": 1})}
    assert before == after


async def test_stale_cache_used_when_refetch_fails(client, staff_user, auth_header, monkeypatch):
    from app.models.job_apply import JobPostingCache

    from app.job_apply.fetch import FETCHER_VERSION

    await JobPostingCache(
        url="https://boards.greenhouse.io/acme/jobs/9", text=REMOTE_JD, source="ats_api",
        fetchedAt=datetime.utcnow() - timedelta(days=2), fetcher=FETCHER_VERSION,
    ).insert()
    monkeypatch.setattr(worker, "FETCH", failing_fetch)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    gh = (await rows_by_url(client, h, run_id))["https://boards.greenhouse.io/acme/jobs/9"]
    assert gh["status"] == "extracted" and gh["jdSource"] == "ats_api"


async def test_cache_from_older_fetcher_is_refetched(client, staff_user, auth_header, monkeypatch):
    """A page cached before closed-job detection (e.g. a 404 page read as the job) isn't reused, however fresh."""
    from app.models.job_apply import JobPostingCache

    await JobPostingCache.get_motor_collection().insert_one({
        "url": "https://boards.greenhouse.io/acme/jobs/9", "text": "We couldn't find this job. " * 20,
        "source": "browser", "fetchedAt": datetime.utcnow() - timedelta(hours=1), "title": "", "company": "",
    })

    async def closed(url, *, client, browser=None):
        raise JobClosed("job closed (HTTP 404)")

    monkeypatch.setattr(worker, "FETCH", closed)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    gh = (await rows_by_url(client, h, run_id))["https://boards.greenhouse.io/acme/jobs/9"]
    assert gh["screen"] == "closed"


async def test_start_market_without_jobs_400(client, staff_user, auth_header):
    h = auth_header(staff_user)
    gb = await make_account(staff_user, country="GB", name="UK")
    run_id = await screened_run(client, h)
    r = await start(client, h, run_id, {"UKEU": [gb]}, [gb])
    assert r.status_code == 400 and "at least one profile" in r.json()["error"]


async def test_rows_carry_markets(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    rows = await rows_by_url(client, h, run_id)
    assert rows["https://jobs.example.com/remote"]["markets"] == ["US"]


async def test_other_location_migration(client, staff_user, auth_header):
    from app.db import _migrate_job_apply_screening
    from app.models.job_apply import JobApplyRow, JobApplyRun

    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    coll = JobApplyRow.get_motor_collection()
    await coll.update_one({"runId": PydanticObjectId(run_id), "url": "https://jobs.example.com/remote"},
                          {"$set": {"groupKey": "CA", "screen": "valid"}})
    await _migrate_job_apply_screening(JobApplyRun.get_motor_collection().database)
    row = await coll.find_one({"runId": PydanticObjectId(run_id), "url": "https://jobs.example.com/remote"})
    assert row["screen"] == "other_location"
    rep = (await client.get(f"/job-applies/runs/{run_id}/screening", headers=h)).json()
    assert rep["others"] == [{"key": "CA", "jobs": 1}] and rep["buckets"]["other_location"] == 1


async def test_stored_urls_are_recleaned_once(client, staff_user, auth_header):
    """Applied history, posting cache and run rows move to the new URL form; two old records that are now the
    same job collapse to one."""
    from app.db import _migrate_job_apply_urls
    from app.job_apply.urls import URL_VERSION
    from app.models.job_apply import JobApplicationLog, JobApplyRow, JobApplyRun, JobPostingCache

    log = JobApplicationLog.get_motor_collection()
    old_apply = "https://jobs.lever.co/acme/1a2b/apply"
    clean = "https://jobs.lever.co/acme/1a2b"
    base = {"userId": staff_user.id, "accountId": "a1", "fileKey": "r1", "appliedAt": datetime.utcnow()}
    await log.insert_one({**base, "url": old_apply})
    await log.insert_one({**base, "url": clean + "?lever-via=x"})
    await log.insert_one({**base, "fileKey": "r2", "url": "https://boards.greenhouse.io/acme/jobs/9?gh_jid=9"})
    await JobPostingCache.get_motor_collection().insert_one(
        {"url": old_apply, "text": "x" * 300, "source": "html", "fetchedAt": datetime.utcnow()})
    run_id = await screened_run(client, auth_header(staff_user))
    rows = JobApplyRow.get_motor_collection()
    await rows.update_one({"runId": PydanticObjectId(run_id), "url": "https://jobs.example.com/remote"},
                          {"$set": {"url": "https://jobs.example.com/remote?utm_source=sheet"}})

    database = JobApplyRun.get_motor_collection().database
    await _migrate_job_apply_urls(database)
    await _migrate_job_apply_urls(database)  # second start: nothing left to do

    logged = sorted(d["url"] for d in await log.find({"userId": staff_user.id}).to_list(None))
    assert logged == ["https://boards.greenhouse.io/acme/jobs/9", clean]
    assert await log.count_documents({"urlv": {"$ne": URL_VERSION}}) == 0
    assert await JobPostingCache.get_motor_collection().find_one({"url": clean})
    assert await rows.find_one({"runId": PydanticObjectId(run_id), "url": "https://jobs.example.com/remote"})


async def test_text_only_page_lands_in_needs_a_check(client, staff_user, auth_header, monkeypatch):
    async def text_page(url, *, client, browser=None):
        return FetchResult(text=REMOTE_JD, source="html", structured=False)

    monkeypatch.setattr(worker, "FETCH", text_page)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    gh = (await rows_by_url(client, h, run_id))["https://boards.greenhouse.io/acme/jobs/9"]
    assert gh["screen"] == "check" and gh["jdStructured"] is False
    remote = (await rows_by_url(client, h, run_id))["https://jobs.example.com/remote"]
    assert remote["jdStructured"] is True  # the description came from the sheet


async def test_sheet_preview_counts(client, staff_user, auth_header):
    data = sheet([["URL"], ["https://jobs.lever.co/acme/1a2b/apply"], ["https://jobs.lever.co/acme/1a2b"],
                  ["https://www.linkedin.com/jobs/view/123"], ["https://a.com/jobs/2?utm_source=x"]])
    r = await client.post("/job-applies/sheet-preview", headers=auth_header(staff_user),
                          files={"file": ("jobs.xlsx", data, "application/octet-stream")})
    body = r.json()
    assert body["total"] == 3 and body["links"] == 4 and body["duplicates"] == 1 and body["cleaned"] == 2
    assert body["blocked"] == 1


async def test_runs_list_summary(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user)
    screened_id = await screened_run(client, h)
    scored_id = await screened_run(client, h)
    await start(client, h, scored_id, {"US": [us]}, [us])
    await worker.run_worker(PydanticObjectId(scored_id))
    rows = await rows_by_url(client, h, scored_id, view="suggested")
    remote = rows["https://jobs.example.com/remote"]
    r = await client.put(f"/job-applies/rows/{remote['_id']}/applied", headers=h,
                         json={"accountId": str(us.id), "resumeId": remote["suggestions"][0]["resumeId"], "applied": True})
    assert r.status_code == 200, r.text

    runs = {x["_id"]: x for x in (await client.get("/job-applies/runs", headers=h)).json()["runs"]}
    s = runs[screened_id]["summary"]
    assert s["worth"] == 2 and s["closed"] == 0 and s["markets"] == {"US": 2, "UKEU": 0, "LATAM": 0}
    t = runs[scored_id]["summary"]
    assert t["applied"] == 1 and t["toApply"] == len(rows) - 1 and t["tailoring"] == 0


async def test_runs_list_survives_a_summary_failure(client, staff_user, auth_header, monkeypatch):
    from app.routers import job_applies as router_mod

    h = auth_header(staff_user)
    await screened_run(client, h)

    def boom(key):
        raise RuntimeError("aggregation trouble")

    monkeypatch.setattr(router_mod, "markets_for", boom)
    r = await client.get("/job-applies/runs", headers=h)
    assert r.status_code == 200 and r.json()["runs"][0]["summary"] is None


# ---------- back to step 2: change the picks and score again ----------


async def scored_run(client, h, assignments, accounts) -> str:
    run_id = await screened_run(client, h)
    assert (await start(client, h, run_id, assignments, accounts)).status_code == 200
    await worker.run_worker(PydanticObjectId(run_id))
    return run_id


async def test_rescore_with_new_picks(client, staff_user, auth_header):
    h = auth_header(staff_user)
    us = await make_account(staff_user, name="US")
    us2 = await make_account(staff_user, name="US 2")
    run_id = await scored_run(client, h, {"US": [us]}, [us])
    r = await start(client, h, run_id, {"US": [us2]}, [us2])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued" and r.json()["phase"] == "score"
    await worker.run_worker(PydanticObjectId(run_id))
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["status"] == "done" and run["counts"]["scored"] == 2
    rows = await rows_by_url(client, h, run_id)
    detail = (await client.get(f"/job-applies/rows/{rows['https://jobs.example.com/remote']['_id']}", headers=h)).json()
    assert {s["accountId"] for s in detail["scores"]} == {str(us2.id)}  # the old profile's scores are gone
    assert rows["https://jobs.example.com/hybrid"]["status"] == "excluded"  # never scored, untouched


async def test_rescore_keeps_tailored_applied_and_exported(client, staff_user, auth_header):
    from app.models.job_apply import JobApplyRow

    h = auth_header(staff_user)
    us = await make_account(staff_user, name="US")
    us2 = await make_account(staff_user, name="US 2")
    run_id = await scored_run(client, h, {"US": [us]}, [us])
    remote = (await rows_by_url(client, h, run_id))["https://jobs.example.com/remote"]
    rid, acc, tailored = PydanticObjectId(remote["_id"]), str(us.id), PydanticObjectId()
    kept = {
        "appliedResumes": [{"accountId": acc, "resumeId": str(us.resumes[0].id), "at": datetime.utcnow()}],
        f"tailored.{acc}": tailored,
        f"exported.{acc}": datetime.utcnow(),
        f"exportedSheets.{acc}.s": {"tab": "2026-10-03", "row": 2},
    }
    await JobApplyRow.get_motor_collection().update_one({"_id": rid}, {"$set": kept})
    assert (await start(client, h, run_id, {"US": [us2]}, [us2])).status_code == 200  # `us` is no longer picked
    await worker.run_worker(PydanticObjectId(run_id))
    row = await JobApplyRow.get(rid)
    assert [m.accountId for m in row.appliedResumes] == [acc]
    assert row.tailored == {acc: tailored} and acc in row.exported and row.exportedSheets[acc]["s"]["row"] == 2


# ---------- runs are kept 7 days ----------


async def test_migration_keeps_runs_seven_days(client, staff_user, auth_header):
    from app.db import _migrate_run_retention
    from app.models.job_apply import JobApplyRow, JobApplyRun

    h = auth_header(staff_user)
    rid = PydanticObjectId(await screened_run(client, h))
    created = datetime.utcnow().replace(microsecond=0) - timedelta(days=2)
    await JobApplyRun.get_motor_collection().update_one(
        {"_id": rid}, {"$set": {"createdAt": created, "expiresAt": created + timedelta(days=3)}})
    rows = JobApplyRow.get_motor_collection()
    await rows.update_many({"runId": rid}, {"$set": {"createdAt": created, "expiresAt": created + timedelta(days=3)}})
    longer = await rows.find_one({"runId": rid})
    await rows.update_one({"_id": longer["_id"]}, {"$set": {"expiresAt": created + timedelta(days=30)}})

    database = JobApplyRun.get_motor_collection().database
    await _migrate_run_retention(database)
    await _migrate_run_retention(database)  # second start: nothing left to do

    week = created + timedelta(days=7)
    assert (await JobApplyRun.get(rid)).expiresAt == week
    after = {r["_id"]: r["expiresAt"] for r in await rows.find({"runId": rid}).to_list(None)}
    assert after.pop(longer["_id"]) == created + timedelta(days=30)  # never shortened
    assert after and set(after.values()) == {week}


async def test_runs_list_goes_back_past_fifty(client, staff_user, auth_header):
    from app.models.job_apply import JobApplyRun

    h = auth_header(staff_user)
    rid = PydanticObjectId(await screened_run(client, h))
    coll = JobApplyRun.get_motor_collection()
    base = await coll.find_one({"_id": rid})
    await coll.insert_many([{**base, "_id": PydanticObjectId()} for _ in range(60)])
    runs = (await client.get("/job-applies/runs", headers=h)).json()["runs"]
    assert len(runs) == 61


# ---------- shared per-URL corrections (JobInfo) ----------

GH = "https://boards.greenhouse.io/acme/jobs/9"


async def put_info(client, h, row_id, **body):
    return await client.put(f"/job-applies/rows/{row_id}/info", headers=h, json=body)


async def test_correction_fixes_a_job_that_could_not_be_fetched(client, staff_user, auth_header, monkeypatch):
    monkeypatch.setattr(worker, "FETCH", failing_fetch)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    gh = (await rows_by_url(client, h, run_id))[GH]
    assert gh["screen"] == "not_fetched"

    res = await put_info(client, h, gh["_id"], jdText=REMOTE_JD)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["screen"] in ("valid", "check") and out["status"] == "extracted"
    assert out["humanEdited"] is True and out["extractionSource"] == "human" and out["jdSource"] == "manual"
    assert out["jdText"] == REMOTE_JD and out["info"]["editedBy"] == staff_user.name
    run = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()
    assert run["counts"]["failed"] == 0


async def test_field_correction_moves_the_job_without_an_llm_call(client, staff_user, auth_header, monkeypatch):
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    hybrid = (await rows_by_url(client, h, run_id))["https://jobs.example.com/hybrid"]
    assert hybrid["screen"] == "onsite"

    async def no_llm(*a, **k):
        raise AssertionError("a field correction must not call the LLM")

    monkeypatch.setattr(worker, "EXTRACT", no_llm)
    res = await put_info(client, h, hybrid["_id"], workMode="remote", title="Corrected Title", company="Acme")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["screen"] in ("valid", "check") and out["workMode"] == "remote"
    assert out["title"] == "Corrected Title" and out["company"] == "Acme" and out["humanEdited"] is True


async def test_correction_is_used_by_another_users_run_with_no_fetch_or_llm(
    client, staff_user, staff_user_2, auth_header, monkeypatch
):
    monkeypatch.setattr(worker, "FETCH", failing_fetch)
    h1 = auth_header(staff_user)
    run1 = await screened_run(client, h1)
    gh = (await rows_by_url(client, h1, run1))[GH]
    assert (await put_info(client, h1, gh["_id"], jdText=REMOTE_JD, title="Fixed By One")).status_code == 200

    fetched, read = [], []

    async def spy_fetch(url, *, client, browser=None):
        fetched.append(url)
        raise FetchError("no job text on the page")

    async def spy_extract(jd, *, user, run_date, llm=None):
        read.append(jd)
        return await fake_extract(jd, user=user, run_date=run_date)

    monkeypatch.setattr(worker, "FETCH", spy_fetch)
    monkeypatch.setattr(worker, "EXTRACT", spy_extract)
    h2 = auth_header(staff_user_2)
    run2 = await screened_run(client, h2)
    row = (await rows_by_url(client, h2, run2))[GH]
    assert GH not in fetched and read == []  # the corrected link is neither fetched nor read by the LLM again
    assert row["title"] == "Fixed By One" and row["humanEdited"] is True and row["screen"] in ("valid", "check")


async def test_machine_reading_never_overwrites_a_correction(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run1 = await screened_run(client, h)
    row = (await rows_by_url(client, h, run1))["https://jobs.example.com/remote"]
    assert (await put_info(client, h, row["_id"], title="Human Title")).status_code == 200
    run2 = await screened_run(client, h)
    again = (await rows_by_url(client, h, run2))["https://jobs.example.com/remote"]
    assert again["title"] == "Human Title" and again["humanEdited"] is True


async def test_correction_needs_a_description_and_an_editor_role(client, staff_user, make_user, auth_header, monkeypatch):
    monkeypatch.setattr(worker, "FETCH", failing_fetch)
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    gh = (await rows_by_url(client, h, run_id))[GH]
    assert (await put_info(client, h, gh["_id"], title="No JD yet")).status_code == 400
    accountant = await make_user(name="Acc", email="acc@example.com", role="accountant")
    assert (await put_info(client, auth_header(accountant), gh["_id"], jdText=REMOTE_JD)).status_code == 403
    assert (await put_info(client, h, gh["_id"], workMode="sideways", jdText=REMOTE_JD)).status_code == 422


async def test_reset_drops_the_correction_and_reads_the_job_again(client, staff_user, auth_header):
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    row = (await rows_by_url(client, h, run_id))["https://jobs.example.com/hybrid"]
    assert (await put_info(client, h, row["_id"], workMode="remote")).status_code == 200
    assert (await client.delete(f"/job-applies/rows/{row['_id']}/info", headers=h)).status_code == 200
    await worker.run_worker(PydanticObjectId(run_id))
    detail = (await client.get(f"/job-applies/rows/{row['_id']}", headers=h)).json()
    assert detail["humanEdited"] is False and detail["info"] is None
    assert detail["jdSource"] == "ats_api" and detail["extractionSource"] in ("llm", "cache")


async def test_open_run_picks_up_a_correction_made_by_someone_else(
    client, staff_user, staff_user_2, auth_header, monkeypatch
):
    monkeypatch.setattr(worker, "FETCH", failing_fetch)
    h1, h2 = auth_header(staff_user), auth_header(staff_user_2)
    run1 = await screened_run(client, h1)
    run2 = await screened_run(client, h2)  # read the link before anyone corrected it
    mine = (await rows_by_url(client, h1, run1))[GH]
    theirs = (await rows_by_url(client, h2, run2))[GH]
    assert theirs["screen"] == "not_fetched"
    assert (await put_info(client, h1, mine["_id"], jdText=REMOTE_JD, title="Fixed By One")).status_code == 200

    stale = (await client.get(f"/job-applies/rows/{theirs['_id']}", headers=h2)).json()
    assert stale["info"]["applied"] is False  # their row was read before the correction

    async def no_io(*a, **k):
        raise AssertionError("syncing a correction must not fetch or call the LLM")

    monkeypatch.setattr(worker, "FETCH", no_io)
    monkeypatch.setattr(worker, "EXTRACT", no_io)
    res = await client.post(f"/job-applies/runs/{run2}/sync-info", headers=h2)
    assert res.status_code == 200 and res.json() == {"updated": 1}
    now = (await client.get(f"/job-applies/rows/{theirs['_id']}", headers=h2)).json()
    assert now["title"] == "Fixed By One" and now["humanEdited"] is True and now["screen"] in ("valid", "check")
    assert now["jdText"] == REMOTE_JD and now["info"]["applied"] is True
    run = (await client.get(f"/job-applies/runs/{run2}", headers=h2)).json()
    assert run["counts"]["failed"] == 0
    again = await client.post(f"/job-applies/runs/{run2}/sync-info", headers=h2)
    assert again.json() == {"updated": 0}
