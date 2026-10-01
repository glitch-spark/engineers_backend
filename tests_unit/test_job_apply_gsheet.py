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
