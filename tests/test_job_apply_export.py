"""Export a run to a shared Google Sheet: one row per job × profile with a working resume link (Google stubbed)."""
import json

import pytest
from beanie import PydanticObjectId

from app import google_sheets, resume_files
from app.job_apply import worker
from app.models import Account, ResumeApplication, UserExtra
from app.models.account import Resume
from tests.test_job_applies import GSHEET, SHEET, STRONG, create_screen_run, fake_extract, fake_fetch, screened_run  # noqa: F401  (fixtures reused)


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(worker, "EXTRACT", fake_extract)
    monkeypatch.setattr(worker, "FETCH", fake_fetch)


@pytest.fixture
def sheets(monkeypatch):
    # values: tab -> rows written with update_range from A1 (headers, the Checks tab); updates / cleared: every call
    state = {"tabs": {}, "rows": [], "error": None, "values": {}, "updates": [], "cleared": [], "checkbox": {}}

    async def info(client, sid):
        if state["error"]:
            raise google_sheets.SheetsError(state["error"])
        return {"title": "Applies", "tabs": dict(state["tabs"])}

    async def ensure(client, sid, title, header, info_, checkbox=None):
        if not title.startswith("Checks · "):
            state["header"] = header  # the application rows' tab
        if title in state["tabs"]:
            return False
        state["tabs"][title] = 1
        state["checkbox"][title] = checkbox
        return True

    def split(a1):
        tab, cell = a1[1:].split("'!", 1)
        return tab.replace("''", "'"), cell

    async def update(client, sid, a1, values):
        state["updates"].append((a1, values))
        tab, cell = split(a1)
        if cell == "A1":
            state["values"][tab] = [list(v) for v in values]

    async def clear(client, sid, a1):
        state["cleared"].append(a1)
        tab, _ = split(a1)
        state["values"][tab] = state["values"].get(tab, [])[:1]

    async def append(client, sid, tab, rows):
        first = len(state["rows"]) + 2  # row 1 is the header
        state["rows"].extend(rows)
        state["tab"] = tab
        return f"'{tab}'!A{first}:J{first + len(rows) - 1}"

    monkeypatch.setattr(google_sheets, "sheet_info", info)
    monkeypatch.setattr(google_sheets, "ensure_tab", ensure)
    monkeypatch.setattr(google_sheets, "append_rows", append)
    monkeypatch.setattr(google_sheets, "update_range", update)
    monkeypatch.setattr(google_sheets, "clear_range", clear, raising=False)
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
    from app.routers.job_applies import EXPORT_HEADER

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)

    preview = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET, "dryRun": True})).json()
    assert preview["ready"] == 3 and preview["waiting"] == 1 and preview["alreadyExported"] == 0  # Tharun x greenhouse waits
    assert preview["sheetTitle"] == "Applies" and preview["tab"].startswith("Apply · ")
    assert preview["serviceAccount"] == "exporter@proj.iam.gserviceaccount.com" and sheets["rows"] == []

    r = await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 3 and body["waiting"] == 1 and body["tab"] == sheets["tab"]
    assert sheets["header"] == EXPORT_HEADER == ["Profile", "Company Name", "Job Title", "Job URL", "Download Resume"]
    assert sheets["checkbox"][sheets["tab"]] is None
    by = {(row[0], row[3]): row for row in sheets["rows"]}
    assert set(by) == {("Tharun", "https://jobs.example.com/remote"), ("Eric", "https://jobs.example.com/remote"),
                       ("Eric", "https://boards.greenhouse.io/acme/jobs/9")}
    assert all(len(row) == 5 and row[4].startswith('=HYPERLINK("') and "/job-applies/files/" in row[4] for row in by.values())

    again = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={})).json()  # saved link reused
    assert again["added"] == 0 and again["alreadyExported"] == 3 and again["waiting"] == 1
    assert (await UserExtra.get(staff_user.id)).jobApplySheetUrl == GSHEET
    listed = (await client.get(f"/job-applies/runs/{run_id}/rows?view=suggested", headers=h)).json()["rows"]
    remote = next(x for x in listed if x["url"] == "https://jobs.example.com/remote")
    assert set(remote["exportedProfiles"]) == {str(tharun.id), str(eric.id)}


async def test_resume_links_work_without_login(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, *_ = await _run(client, h, staff_user)
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    links = {(row[0], row[3]): row[4].split('"')[1] for row in sheets["rows"]}
    path = lambda url: url[url.index("/job-applies/files/"):]  # noqa: E731

    pdf = await client.get(path(links[("Tharun", "https://jobs.example.com/remote")]))  # no Authorization header
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    redirect = await client.get(path(links[("Eric", "https://jobs.example.com/remote")]), follow_redirects=False)
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
    assert sorted(w["row"] for w in placed.values()) == [2, 3, 4]
    for (acc, url), where in placed.items():
        sheet_row = sheets["rows"][where["row"] - 2]
        assert sheet_row[3] == url and sheet_row[0] == ("Tharun" if acc == str(tharun.id) else "Eric")
    assert all(w["tab"] == sheets["tab"] and w["sheetId"] for w in placed.values())


async def _tailor_and_finish(client, h, row_id, account_id, pdf="JVBERi0xLjc="):
    t = (await client.post(f"/job-applies/rows/{row_id}/tailor", headers=h, json={"accountId": account_id})).json()["tailored"]
    await ResumeApplication.get_motor_collection().update_one(
        {"_id": PydanticObjectId(t["jobId"])}, {"$set": {"status": "completed", "step": "done", "pdfData": pdf}})
    return await ResumeApplication.get(PydanticObjectId(t["jobId"]))


async def test_tailor_only_rows_wait_for_tailoring(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    assert ("Tharun", gh["url"]) not in {(r[0], r[3]) for r in sheets["rows"]}
    await _tailor_and_finish(client, h, gh["_id"], str(tharun.id))
    body = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={})).json()
    assert body["added"] == 1 and body["waiting"] == 0 and body["alreadyExported"] == 3
    added = sheets["rows"][-1]
    assert (added[0], added[3]) == ("Tharun", gh["url"]) and added[4].startswith('=HYPERLINK("')


async def test_resume_still_tailoring_is_not_exported(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    await client.post(f"/job-applies/rows/{gh['_id']}/tailor", headers=h, json={"accountId": str(tharun.id)})  # queued
    body = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})).json()
    assert body["added"] == 3 and body["waiting"] == 1
    assert ("Tharun", gh["url"]) not in {(r[0], r[3]) for r in sheets["rows"]}


# ---------- a re-tailored resume updates its exported row ----------


@pytest.fixture
def sheet_cells(monkeypatch, sheets):
    """read_range serves what the export appended (header on row 1); update_range writes into it."""
    from app.routers.job_applies import EXPORT_HEADER

    writes = []
    base_update = google_sheets.update_range  # the sheets fixture's: headers and the Checks tab

    async def read(client, sid, a1):
        return [EXPORT_HEADER, *sheets["rows"]]

    async def update(client, sid, a1, values):
        if "!E" not in a1:
            return await base_update(client, sid, a1, values)
        writes.append((a1, values))
        n = int(a1.split("!E")[1].split(":")[0])
        sheets["rows"][n - 2][4] = values[0][0]

    monkeypatch.setattr(google_sheets, "read_range", read)
    monkeypatch.setattr(google_sheets, "update_range", update)
    return writes


async def test_retailored_resume_updates_its_exported_row(client, staff_user, auth_header, sheets, sheet_cells):
    from app.job_apply import sheet_links

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    first = await _tailor_and_finish(client, h, gh["_id"], str(tharun.id))
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    row = next(r for r in sheets["rows"] if (r[0], r[3]) == ("Tharun", gh["url"]))
    before = row[4]
    second = await _tailor_and_finish(client, h, gh["_id"], str(tharun.id))
    assert await sheet_links.fill_link(second) is True
    assert await sheet_links.fill_link(first) is False  # an older resume finishing late doesn't win
    assert len(sheet_cells) == 1 and row[4] != before and row[4].startswith('=HYPERLINK("')


async def test_fill_link_sheet_error_is_swallowed(client, staff_user, auth_header, sheets, sheet_cells, monkeypatch):
    from app.job_apply import sheet_links

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    await _tailor_and_finish(client, h, gh["_id"], str(tharun.id))
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})

    async def broken(client, sid, a1):
        raise google_sheets.SheetsError("The app can't edit that sheet.")

    monkeypatch.setattr(google_sheets, "read_range", broken)
    job = await _tailor_and_finish(client, h, gh["_id"], str(tharun.id))
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


async def test_export_escapes_text_that_looks_like_a_formula(client, staff_user, auth_header, sheets):
    from app.models.job_apply import JobApplyRow

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    gh = rows["https://boards.greenhouse.io/acme/jobs/9"]
    await JobApplyRow.get_motor_collection().update_one(
        {"_id": PydanticObjectId(gh["_id"])},
        {"$set": {"title": '=HYPERLINK("https://evil.example","Apply here")', "company": "+Acme"}})
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    mine = [r for r in sheets["rows"] if r[3] == gh["url"]]
    assert mine and all(r[2] == """'=HYPERLINK("https://evil.example","Apply here")""" and r[1] == "'+Acme" for r in mine)
    assert all(r[4].startswith("=HYPERLINK(") for r in sheets["rows"])  # our own links stay live


async def test_counts_say_what_is_left_to_export(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    before = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["applications"]
    assert before["toExport"] == 3 and before["toGo"] == 4  # the 4th waits for tailoring
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    after = (await client.get(f"/job-applies/runs/{run_id}", headers=h)).json()["applications"]
    assert after["toExport"] == 0 and after["toGo"] == 4


async def test_step3_export_leaves_the_checks_tab_alone(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    body = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})).json()
    assert "checksTab" not in body and not any(tab.startswith("Checks · ") for tab in sheets["values"])


# ---------- what the checks found, for validating them ----------


def _checks_tab(sheets):
    tabs = [t for t in sheets["values"] if t.startswith("Checks · ")]
    assert len(tabs) == 1, sheets["values"].keys()
    return tabs[0], sheets["values"][tabs[0]]


async def test_export_refreshes_the_header_of_an_existing_tab(client, staff_user, auth_header, sheets):
    from app.routers.job_applies import EXPORT_HEADER

    h = auth_header(staff_user)
    run_id, rows, tharun, eric, t = await _run(client, h, staff_user)
    tab = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h,
                             json={"sheetUrl": GSHEET, "dryRun": True})).json()["tab"]
    sheets["tabs"][tab] = 1  # made by an export before these columns existed
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    assert sheets["values"][tab] == [EXPORT_HEADER]


async def test_export_checks_before_scoring(client, staff_user, auth_header, sheets):
    from app.job_apply.sheet_checks import CHECKS_HEADER

    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    r = await client.post(f"/job-applies/runs/{run_id}/export-checks", headers=h, json={"sheetUrl": GSHEET})
    assert r.status_code == 200, r.text
    tab, values = _checks_tab(sheets)
    assert r.json()["tab"] == tab and r.json()["jobs"] == 3 and tab.startswith("Checks · jobs.xlsx · ")
    assert values[0] == CHECKS_HEADER and len(values) == 4
    by_url = {v[1]: dict(zip(CHECKS_HEADER, v)) for v in values[1:]}
    hybrid = by_url["https://jobs.example.com/hybrid"]
    assert hybrid["Status"] == "On-site / hybrid" and hybrid["Remote/Hybrid/Onsite"] == "Hybrid"
    gh = by_url["https://boards.greenhouse.io/acme/jobs/9"]
    assert gh["Posting Date"] == "2026-09-28" and gh["Location"] == "US" and gh["Security Clearance"] == "None"
    assert all(len(v) == 8 for v in values)
    assert sheets["checkbox"][tab] is None and sheets["rows"] == []  # no checkboxes, no application rows
    again = await client.post(f"/job-applies/runs/{run_id}/export-checks", headers=h, json={})
    assert again.status_code == 200 and again.json()["sheetUrl"] == GSHEET  # the link is remembered


async def test_export_checks_twice_overwrites_the_tab(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    for _ in range(2):
        assert (await client.post(f"/job-applies/runs/{run_id}/export-checks", headers=h, json={"sheetUrl": GSHEET})).status_code == 200
    tab, values = _checks_tab(sheets)
    assert len(values) == 4 and len([c for c in sheets["cleared"] if tab in c]) == 2


async def test_export_checks_clears_the_whole_tab(client, staff_user, auth_header, sheets):
    """A tab written by an earlier, wider version of the export loses its old header and extra columns."""
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    await client.post(f"/job-applies/runs/{run_id}/export-checks", headers=h, json={"sheetUrl": GSHEET})
    tab, _ = _checks_tab(sheets)
    assert sheets["cleared"] == [f"'{tab}'!A1:Z"]


async def test_export_checks_dry_run_writes_nothing(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    body = (await client.post(f"/job-applies/runs/{run_id}/export-checks", headers=h,
                              json={"sheetUrl": GSHEET, "dryRun": True})).json()
    assert body["jobs"] == 3 and body["tab"].startswith("Checks · ") and body["sheetTitle"] == "Applies"
    assert sheets["values"] == {} and sheets["updates"] == []


async def test_export_checks_waits_for_the_check(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id = (await create_screen_run(client, h)).json()["runId"]  # queued: the jobs aren't checked yet
    r = await client.post(f"/job-applies/runs/{run_id}/export-checks", headers=h, json={"sheetUrl": GSHEET})
    assert r.status_code == 409 and "still being checked" in r.json()["error"]


async def test_export_checks_needs_a_sheet_link(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id = await screened_run(client, h)
    r = await client.post(f"/job-applies/runs/{run_id}/export-checks", headers=h, json={})
    assert r.status_code == 400 and "Paste the Google Sheet link" in r.json()["error"]
