"""Export a run to a shared Google Sheet: one row per job × profile with a working resume link (Google stubbed)."""
import json

import pytest
from beanie import PydanticObjectId

from app import google_sheets, resume_files
from app.job_apply import worker
from app.models import Account, ResumeApplication, UserExtra
from app.models.account import Resume
from tests.test_job_applies import GSHEET, SHEET, STRONG, fake_extract, fake_fetch  # noqa: F401  (fixtures reused)


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(worker, "EXTRACT", fake_extract)
    monkeypatch.setattr(worker, "FETCH", fake_fetch)


@pytest.fixture
def sheets(monkeypatch):
    state = {"tabs": {}, "rows": [], "error": None}

    async def info(client, sid):
        if state["error"]:
            raise google_sheets.SheetsError(state["error"])
        return {"title": "Applies", "tabs": dict(state["tabs"])}

    async def ensure(client, sid, title, header, info_):
        state["header"] = header
        if title in state["tabs"]:
            return False
        state["tabs"][title] = 1
        return True

    async def append(client, sid, tab, rows):
        first = len(state["rows"]) + 2  # row 1 is the header
        state["rows"].extend(rows)
        state["tab"] = tab
        return f"'{tab}'!A{first}:J{first + len(rows) - 1}"

    monkeypatch.setattr(google_sheets, "sheet_info", info)
    monkeypatch.setattr(google_sheets, "ensure_tab", ensure)
    monkeypatch.setattr(google_sheets, "append_rows", append)
    monkeypatch.setattr(google_sheets, "service_account_email", lambda: "exporter@proj.iam.gserviceaccount.com")
    monkeypatch.setattr(resume_files, "presigned_url", lambda key, filename: f"https://s3.example/{key}")
    return state


async def _run(client, h, owner):
    tharun = Account(name="Tharun", country="US", region="US", createdBy=owner.id, resumes=[],
                     styleTemplate="<html><body><h1>T</h1></body></html>")
    await tharun.insert()
    eric = Account(name="Eric", country="US", region="US", createdBy=owner.id,
                   resumes=[Resume(filename="eric.pdf", markdown=STRONG, s3Key="profiles/e/resumes/r.pdf")])
    await eric.insert()
    sel = [{"accountId": str(a.id), "resumeIds": []} for a in (tharun, eric)]
    run_id = (await client.post("/job-applies/runs", headers=h, files={"file": ("jobs.xlsx", SHEET, "application/octet-stream")},
                                data={"selection": json.dumps(sel), "threshold": "60"})).json()["runId"]
    await worker.run_worker(PydanticObjectId(run_id))
    rows = {x["url"]: x for x in (await client.get(f"/job-applies/runs/{run_id}/rows?view=suggested", headers=h)).json()["rows"]}
    # Tharun's tailored resume for the remote job is ready.
    remote = rows["https://jobs.example.com/remote"]
    t = (await client.post(f"/job-applies/rows/{remote['_id']}/tailor", headers=h, json={"accountId": str(tharun.id)})).json()["tailored"]
    await ResumeApplication.get_motor_collection().update_one(
        {"_id": PydanticObjectId(t["jobId"])}, {"$set": {"status": "completed", "step": "done", "pdfData": "JVBERi0xLjc="}}
    )
    return run_id, rows, tharun, eric, t


async def test_export_rows_per_job_and_profile(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)

    preview = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET, "dryRun": True})).json()
    assert preview["ready"] == 4 and preview["alreadyExported"] == 0 and preview["sheetTitle"] == "Applies"
    assert preview["needsResume"] == 1 and preview["pendingLinks"] == 1  # Tharun on the greenhouse job: link later
    assert preview["serviceAccount"] == "exporter@proj.iam.gserviceaccount.com" and sheets["rows"] == []

    r = await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 4 and body["tab"] == sheets["tab"]
    assert sheets["header"][:6] == ["Date", "Profile", "Company", "Title", "Job URL", "Resume"] and sheets["header"][-1] == "Applied"
    by = {(row[1], row[4]): row for row in sheets["rows"]}
    tailored = by[("Tharun", "https://jobs.example.com/remote")]
    assert tailored[5].startswith('=HYPERLINK("') and "/job-applies/files/" in tailored[5] and tailored[6] == "Tailored"
    uploaded = by[("Eric", "https://jobs.example.com/remote")]
    assert uploaded[6] == "Uploaded · eric.pdf" and uploaded[-1] is False
    later = by[("Tharun", "https://boards.greenhouse.io/acme/jobs/9")]
    assert later[5] == "To tailor" and later[6] == "Tailored (pending)"

    again = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={})).json()  # saved link reused
    assert again["added"] == 0 and again["alreadyExported"] == 4
    assert (await UserExtra.get(staff_user.id)).jobApplySheetUrl == GSHEET
    listed = (await client.get(f"/job-applies/runs/{run_id}/rows?view=suggested", headers=h)).json()["rows"]
    remote = next(x for x in listed if x["url"] == "https://jobs.example.com/remote")
    assert set(remote["exportedProfiles"]) == {str(tharun.id), str(eric.id)}


async def test_resume_links_work_without_login(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, *_ = await _run(client, h, staff_user)
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    links = {row[6]: row[5].split('"')[1] for row in sheets["rows"] if row[5].startswith("=HYPERLINK")}
    path = lambda url: url[url.index("/job-applies/files/"):]  # noqa: E731

    pdf = await client.get(path(links["Tailored"]))  # no Authorization header
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    redirect = await client.get(path(links["Uploaded · eric.pdf"]), follow_redirects=False)
    assert redirect.status_code in (302, 307) and redirect.headers["location"] == "https://s3.example/profiles/e/resumes/r.pdf"
    assert (await client.get("/job-applies/files/not-a-token")).status_code == 404


async def test_export_reports_sharing_problem(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, *_ = await _run(client, h, staff_user)
    sheets["error"] = "The app can't edit that sheet. Share it with exporter@proj.iam.gserviceaccount.com as Editor."
    r = await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET, "dryRun": True})
    assert r.status_code == 400 and "Share it with exporter@" in r.json()["error"]
    bad = await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": "https://example.com/x"})
    assert bad.status_code == 400 and "Google Sheets link" in bad.json()["error"]


async def test_export_records_where_each_row_went(client, staff_user, auth_header, sheets):
    from app.models.job_apply import JobApplyRow

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    placed = {}
    for r in await JobApplyRow.find({"runId": PydanticObjectId(run_id)}).to_list():
        for acc, where in (r.exportedRows or {}).items():
            placed[(acc, r.url)] = where
    assert sorted(w["row"] for w in placed.values()) == [2, 3, 4, 5]
    by_row = {w["row"]: k for k, w in placed.items()}
    for n, (acc, url) in by_row.items():
        sheet_row = sheets["rows"][n - 2]
        assert sheet_row[4] == url and sheet_row[1] == ("Tharun" if acc == str(tharun.id) else "Eric")
    assert all(w["tab"] == sheets["tab"] and w["sheetId"] for w in placed.values())


async def test_export_tailor_missing_queues_tailoring(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    body = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h,
                              json={"sheetUrl": GSHEET, "tailorMissing": True})).json()
    assert body["tailorQueued"] == 1 and body["overLimit"] == 0 and body["added"] == 4
    later = next(r for r in sheets["rows"] if r[1] == "Tharun" and r[4] == "https://boards.greenhouse.io/acme/jobs/9")
    assert later[5] == "Tailoring…"
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    detail = (await client.get(f"/job-applies/rows/{gh['_id']}", headers=h)).json()
    assert any(t["accountId"] == str(tharun.id) for t in detail["tailored"])


async def test_export_tailor_missing_over_the_daily_cap(client, staff_user, auth_header, sheets, monkeypatch):
    from app import resume_queue

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)

    async def none_left(user):
        return 0

    monkeypatch.setattr(resume_queue, "remaining_today", none_left)
    body = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h,
                              json={"sheetUrl": GSHEET, "tailorMissing": True})).json()
    assert body["tailorQueued"] == 0 and body["overLimit"] == 1 and body["added"] == 4
    later = next(r for r in sheets["rows"] if r[1] == "Tharun" and r[4] == "https://boards.greenhouse.io/acme/jobs/9")
    assert later[5] == "To tailor"


# ---------- filling the link in when the tailored resume is ready ----------


@pytest.fixture
def sheet_cells(monkeypatch, sheets):
    """read_range serves what the export appended (header on row 1); update_range writes into it."""
    writes = []

    async def read(client, sid, a1):
        return [["Date", "Profile", "Company", "Title", "Job URL", "Resume", "Resume type", "Score", "Posted", "Applied"],
                *sheets["rows"]]

    async def update(client, sid, a1, values):
        writes.append((a1, values))
        n = int(a1.split("!F")[1].split(":")[0])
        sheets["rows"][n - 2][5:7] = values[0]

    monkeypatch.setattr(google_sheets, "read_range", read)
    monkeypatch.setattr(google_sheets, "update_range", update)
    return writes


async def _tailor_and_finish(client, h, row_id, account_id, pdf="JVBERi0xLjc="):
    t = (await client.post(f"/job-applies/rows/{row_id}/tailor", headers=h, json={"accountId": account_id})).json()["tailored"]
    await ResumeApplication.get_motor_collection().update_one(
        {"_id": PydanticObjectId(t["jobId"])}, {"$set": {"status": "completed", "step": "done", "pdfData": pdf}})
    return await ResumeApplication.get(PydanticObjectId(t["jobId"]))


async def test_link_filled_after_tailoring(client, staff_user, auth_header, sheets, sheet_cells):
    from app.job_apply import sheet_links

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    job = await _tailor_and_finish(client, h, gh["_id"], str(tharun.id))
    assert await sheet_links.fill_link(job) is True
    later = next(r for r in sheets["rows"] if r[1] == "Tharun" and r[4] == gh["url"])
    assert later[5].startswith('=HYPERLINK("') and "/job-applies/files/" in later[5] and later[6] == "Tailored"


async def test_link_refilled_after_retailor(client, staff_user, auth_header, sheets, sheet_cells):
    from app.job_apply import sheet_links

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    first = await _tailor_and_finish(client, h, gh["_id"], str(tharun.id))
    await sheet_links.fill_link(first)
    second = await _tailor_and_finish(client, h, gh["_id"], str(tharun.id))
    assert await sheet_links.fill_link(second) is True
    assert await sheet_links.fill_link(first) is False  # an older resume finishing late doesn't win
    assert len(sheet_cells) == 2 and sheet_cells[-1][0] == sheet_cells[0][0]  # the same row, rewritten once


async def test_fill_link_sheet_error_is_swallowed(client, staff_user, auth_header, sheets, sheet_cells, monkeypatch):
    from app.job_apply import sheet_links

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})

    async def broken(client, sid, a1):
        raise google_sheets.SheetsError("The app can't edit that sheet.")

    monkeypatch.setattr(google_sheets, "read_range", broken)
    job = await _tailor_and_finish(client, h, rows["https://boards.greenhouse.io/acme/jobs/9"]["_id"], str(tharun.id))
    assert await sheet_links.fill_link(job) is False


async def test_fill_link_ignores_jobs_never_exported(client, staff_user, auth_header, sheets, sheet_cells):
    from app.job_apply import sheet_links

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    job = await _tailor_and_finish(client, h, rows["https://boards.greenhouse.io/acme/jobs/9"]["_id"], str(tharun.id))
    assert await sheet_links.fill_link(job) is False and sheet_cells == []


# ---------- Generated Resumes knows which run a tailored resume came from ----------


async def test_resume_job_links_to_its_run(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    remote = rows["https://jobs.example.com/remote"]
    one = (await client.get(f"/resume/jobs/{t['jobId']}", headers=h)).json()
    assert one["jobApplyRowId"] == remote["_id"] and one["jobApplyRunId"] == run_id and one["jobApplyRunName"] == "jobs.xlsx"
    listed = (await client.get("/resume/jobs", headers=h)).json()
    item = next(j for j in listed["jobs"] if j["_id"] == t["jobId"])
    assert item["jobApplyRunId"] == run_id and item["jobApplyRunName"] == "jobs.xlsx"


async def test_resume_job_whose_run_expired(client, staff_user, auth_header, sheets):
    from app.models.job_apply import JobApplyRow, JobApplyRun

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    await JobApplyRow.get_motor_collection().delete_many({"runId": PydanticObjectId(run_id)})
    await JobApplyRun.get_motor_collection().delete_one({"_id": PydanticObjectId(run_id)})
    one = (await client.get(f"/resume/jobs/{t['jobId']}", headers=h)).json()
    assert one["jobApplyRowId"] and one["jobApplyRunId"] is None and one["jobApplyRunName"] is None


async def test_link_filled_when_tailoring_finishes_during_export(client, staff_user, auth_header, sheets, sheet_cells, monkeypatch):
    """The resume completes while the export is writing: its fill-in runs before the row is recorded, so the
    export itself fills the link in once it has recorded where the row went."""
    from app.job_apply import sheet_links

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    started = (await client.post(f"/job-applies/rows/{gh['_id']}/tailor", headers=h, json={"accountId": str(tharun.id)})).json()["tailored"]
    job_id = PydanticObjectId(started["jobId"])
    append = google_sheets.append_rows

    async def append_while_finishing(http, sid, tab, values):
        written = await append(http, sid, tab, values)
        await ResumeApplication.get_motor_collection().update_one(
            {"_id": job_id}, {"$set": {"status": "completed", "step": "done", "pdfData": "JVBERi0xLjc="}})
        assert await sheet_links.fill_link(await ResumeApplication.get(job_id)) is False  # not recorded yet
        return written

    monkeypatch.setattr(google_sheets, "append_rows", append_while_finishing)
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    later = next(r for r in sheets["rows"] if r[1] == "Tharun" and r[4] == gh["url"])
    assert later[5].startswith('=HYPERLINK("') and later[6] == "Tailored"


async def test_export_escapes_text_that_looks_like_a_formula(client, staff_user, auth_header, sheets):
    from app.models.job_apply import JobApplyRow

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    await JobApplyRow.get_motor_collection().update_one(
        {"_id": PydanticObjectId(gh["_id"])},
        {"$set": {"title": '=HYPERLINK("https://evil.example","Apply here")', "company": "+Acme"}})
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    mine = [r for r in sheets["rows"] if r[4] == gh["url"]]
    assert mine and all(r[3] == """'=HYPERLINK("https://evil.example","Apply here")""" and r[2] == "'+Acme" for r in mine)
    assert all(r[5].startswith(("=HYPERLINK(", "To tailor", "Tailoring")) for r in sheets["rows"])  # our own links stay live


async def test_counts_say_what_is_left_to_export(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    before = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["applications"]
    assert before["toExport"] == before["toGo"] == 4
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    after = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["applications"]
    assert after["toExport"] == 0 and after["toGo"] == 4
