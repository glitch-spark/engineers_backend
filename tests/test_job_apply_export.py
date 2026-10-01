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
        state["rows"].extend(rows)
        state["tab"] = tab

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
    assert preview["ready"] == 3 and preview["alreadyExported"] == 0 and preview["sheetTitle"] == "Applies"
    assert preview["needsResume"] == 1  # Tharun on the greenhouse job: not tailored yet
    assert preview["serviceAccount"] == "exporter@proj.iam.gserviceaccount.com" and sheets["rows"] == []

    r = await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 3 and body["tab"] == sheets["tab"]
    assert sheets["header"][:6] == ["Date", "Profile", "Company", "Title", "Job URL", "Resume"] and sheets["header"][-1] == "Applied"
    by = {(row[1], row[4]): row for row in sheets["rows"]}
    tailored = by[("Tharun", "https://jobs.example.com/remote")]
    assert tailored[5].startswith('=HYPERLINK("') and "/job-applies/files/" in tailored[5] and tailored[6] == "Tailored"
    uploaded = by[("Eric", "https://jobs.example.com/remote")]
    assert uploaded[6] == "Uploaded · eric.pdf" and uploaded[-1] is False

    again = (await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={})).json()  # saved link reused
    assert again["added"] == 0 and again["alreadyExported"] == 3
    assert (await UserExtra.get(staff_user.id)).jobApplySheetUrl == GSHEET
    listed = (await client.get(f"/job-applies/runs/{run_id}/rows?view=suggested", headers=h)).json()["rows"]
    remote = next(x for x in listed if x["url"] == "https://jobs.example.com/remote")
    assert set(remote["exportedProfiles"]) == {str(tharun.id), str(eric.id)}


async def test_resume_links_work_without_login(client, staff_user, auth_header, sheets):
    h = auth_header(staff_user)
    run_id, *_ = await _run(client, h, staff_user)
    await client.post(f"/job-applies/runs/{run_id}/export-sheet", headers=h, json={"sheetUrl": GSHEET})
    links = {row[6]: row[5].split('"')[1] for row in sheets["rows"]}
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
