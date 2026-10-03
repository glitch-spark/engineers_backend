"""Unit tests for reading a link-shared Google Sheet (httpx.MockTransport, no network)."""
import io

import httpx
import pytest
from openpyxl import Workbook

from app.job_apply.gsheet import download_gsheet, parse_gsheet_url
from app.job_apply.sheet import SheetError

SID = "1AbC_dEf-123"
JD = "We are hiring a backend engineer. " * 10


def test_parse_gsheet_url_forms():
    assert parse_gsheet_url(f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=42") == (SID, "42")
    assert parse_gsheet_url(f"https://docs.google.com/spreadsheets/d/{SID}/edit?gid=7#gid=7") == (SID, "7")
    assert parse_gsheet_url(f"https://docs.google.com/spreadsheets/d/{SID}") == (SID, None)
    assert parse_gsheet_url(f"  https://docs.google.com/spreadsheets/u/1/d/{SID}/view  ") == (SID, None)


@pytest.mark.parametrize("bad", ["", "https://example.com/spreadsheets/d/x", "https://docs.google.com/document/d/x/edit", "not a url"])
def test_parse_gsheet_url_rejects(bad):
    with pytest.raises(SheetError, match="Google Sheets link"):
        parse_gsheet_url(bad)


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_download_csv_of_linked_tab():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        csv = f"url,description\nhttps://a.com/1,\"{JD}\"\n".encode()
        return httpx.Response(200, content=csv, headers={
            "content-type": "text/csv", "content-disposition": "attachment; filename=\"Jobs Oct - Sheet1.csv\"",
        })

    async with client(handler) as c:
        title, rows = await download_gsheet(f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=42", client=c)
    assert seen == {"format": "csv", "gid": "42"}
    assert title == "Jobs Oct - Sheet1" and [r.url for r in rows] == ["https://a.com/1"]


async def test_private_sheet_gives_sharing_hint():
    def handler(request):
        if request.url.host == "docs.google.com":
            return httpx.Response(302, headers={"location": "https://accounts.google.com/ServiceLogin?continue=x"})
        return httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html"})

    async with client(handler) as c:
        with pytest.raises(SheetError, match="Anyone with the link"):
            await download_gsheet(f"https://docs.google.com/spreadsheets/d/{SID}/edit", client=c)


async def test_unknown_tab_falls_back_to_first_tab():
    # A link can name a tab that no longer exists (e.g. gid=0 after the first tab was deleted): the browser
    # quietly opens the first tab, but the export answers 400 with an HTML "Page Not Found".
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if "gid" in request.url.params:
            return httpx.Response(400, text="<html>Page Not Found</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=f"url,description\nhttps://a.com/1,\"{JD}\"\n".encode(), headers={
            "content-type": "text/csv",
        })

    async with client(handler) as c:
        _, rows = await download_gsheet(f"https://docs.google.com/spreadsheets/d/{SID}/edit?gid=0#gid=0", client=c)
    assert seen == [{"format": "csv", "gid": "0"}, {"format": "csv"}]
    assert [r.url for r in rows] == ["https://a.com/1"]


async def test_missing_sheet():
    async with client(lambda r: httpx.Response(404, text="nope")) as c:
        with pytest.raises(SheetError, match="not found"):
            await download_gsheet(f"https://docs.google.com/spreadsheets/d/{SID}/edit", client=c)


async def test_falls_back_to_xlsx_when_links_are_hidden_behind_text():
    wb = Workbook()
    wb.active.append(["Job", "JD"])
    wb.active.append(['=HYPERLINK("https://x.com/a","Apply")', JD])
    buf = io.BytesIO()
    wb.save(buf)

    def handler(request):
        if request.url.params.get("format") == "csv":
            return httpx.Response(200, content=f"Job,JD\nApply,\"{JD}\"\n".encode(), headers={"content-type": "text/csv"})
        return httpx.Response(200, content=buf.getvalue(), headers={
            "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })

    async with client(handler) as c:
        title, rows = await download_gsheet(f"https://docs.google.com/spreadsheets/d/{SID}/edit", client=c)
    assert [r.url for r in rows] == ["https://x.com/a"] and title == "Google Sheet"


async def test_hidden_links_win_over_a_stray_url_in_the_csv():
    # CSV loses the hyperlinks behind "Apply"; one note cell has a URL. The xlsx (with hyperlinks) has the jobs.
    wb = Workbook()
    wb.active.append(["Job", "JD", "Notes"])
    for i in range(3):
        wb.active.append([f'=HYPERLINK("https://x.com/jobs/{i}","Apply")', JD, "see https://blog.x.com/post" if i == 0 else ""])
    buf = io.BytesIO()
    wb.save(buf)

    def handler(request):
        if request.url.params.get("format") == "csv":
            body = "Job,JD,Notes\n" + "\n".join(
                f'Apply,"{JD}",{"see https://blog.x.com/post" if i == 0 else ""}' for i in range(3))
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/csv"})
        return httpx.Response(200, content=buf.getvalue(), headers={
            "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })

    async with client(handler) as c:
        _, rows = await download_gsheet(f"https://docs.google.com/spreadsheets/d/{SID}/edit", client=c)
    assert [r.url for r in rows] == [f"https://x.com/jobs/{i}" for i in range(3)]


def test_parse_published_link_and_last_gid():
    pub = "2PACX-1vAbc_123xyz"
    assert parse_gsheet_url(f"https://docs.google.com/spreadsheets/d/e/{pub}/pubhtml?gid=7&single=true") == (f"e/{pub}", "7")
    assert parse_gsheet_url(f"https://docs.google.com/spreadsheets/d/e/{pub}/pub?output=csv") == (f"e/{pub}", None)
    # the tab in the address bar (#gid=) is the one the user is looking at
    assert parse_gsheet_url(f"https://docs.google.com/spreadsheets/d/{SID}/edit?gid=0#gid=5") == (SID, "5")
    assert parse_gsheet_url(f"docs.google.com/spreadsheets/d/{SID}/view") == (SID, None)


async def test_download_published_sheet():
    pub = "2PACX-1vAbc_123xyz"
    seen = {}

    def handler(request):
        seen["path"], seen["params"] = request.url.path, dict(request.url.params)
        return httpx.Response(200, content=f"url,description\nhttps://a.com/1,\"{JD}\"\n".encode(),
                              headers={"content-type": "text/csv"})

    async with client(handler) as c:
        _, rows = await download_gsheet(f"https://docs.google.com/spreadsheets/d/e/{pub}/pubhtml?gid=7", client=c)
    assert seen == {"path": f"/spreadsheets/d/e/{pub}/pub", "params": {"output": "csv", "gid": "7"}}
    assert [r.url for r in rows] == ["https://a.com/1"]
