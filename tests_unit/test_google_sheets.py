"""Google Sheets client for the export: service-account auth and tab/rows calls (httpx.MockTransport)."""
import json
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import httpx  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from app import google_sheets  # noqa: E402

SID = "1AbC_dEf-1234567890"


@pytest.fixture
def service_account(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    info = {"client_email": "exporter@proj.iam.gserviceaccount.com", "private_key": pem,
            "token_uri": "https://oauth2.googleapis.com/token"}
    monkeypatch.setattr(google_sheets, "_service_account_raw", lambda: json.dumps(info))
    google_sheets._token_cache.clear()
    return info


def mock(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_not_configured(monkeypatch):
    monkeypatch.setattr(google_sheets, "_service_account_raw", lambda: None)
    async with mock(lambda r: httpx.Response(500)) as c:
        with pytest.raises(google_sheets.SheetsError, match="isn't set up"):
            await google_sheets.sheet_info(c, SID)


async def test_ensure_tab_creates_header_once(service_account):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, req.content.decode() if req.content else ""))
        if req.url.host == "oauth2.googleapis.com":
            assert b"jwt-bearer" in req.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert req.headers["authorization"] == "Bearer tok"
        if req.method == "GET":
            return httpx.Response(200, json={"properties": {"title": "Applies"},
                                             "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]})
        if req.url.path.endswith(":batchUpdate"):
            return httpx.Response(200, json={"replies": [{"addSheet": {"properties": {"sheetId": 77, "title": "2026-10-02"}}}]})
        return httpx.Response(200, json={})

    async with mock(handler) as c:
        info = await google_sheets.sheet_info(c, SID)
        assert info == {"title": "Applies", "tabs": {"Sheet1": 0}}
        assert await google_sheets.ensure_tab(c, SID, "2026-10-02", ["Date", "Applied"], info) is True
        assert await google_sheets.ensure_tab(c, SID, "Sheet1", ["Date"], info) is False
        await google_sheets.append_rows(c, SID, "2026-10-02", [["2026-10-02", False]])
    paths = [p for _, p, _ in calls]
    assert any(p.endswith(":batchUpdate") for p in paths)
    assert any("values/" in p and p.endswith(":append") for p in paths)
    assert sum(1 for p in paths if p.endswith("/token")) == 1  # token cached


async def test_not_shared_gives_share_hint(service_account):
    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(403, json={"error": {"message": "The caller does not have permission"}})

    async with mock(handler) as c:
        with pytest.raises(google_sheets.SheetsError, match="exporter@proj.iam.gserviceaccount.com"):
            await google_sheets.sheet_info(c, SID)


def test_parse_row_span():
    assert google_sheets.parse_row_span("'2026-10-03'!A12:J20") == ("2026-10-03", 12, 20)
    assert google_sheets.parse_row_span("Sheet1!A5:J5") == ("Sheet1", 5, 5)
    assert google_sheets.parse_row_span("'It''s'!A2:J3") == ("It's", 2, 3)


def _sheets(handler_tail):
    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return handler_tail(req)
    return handler


async def test_append_rows_returns_range(service_account):
    def tail(req):
        return httpx.Response(200, json={"updates": {"updatedRange": "'T'!A7:J8"}})

    async with mock(_sheets(tail)) as c:
        assert await google_sheets.append_rows(c, SID, "T", [["a"], ["b"]]) == "'T'!A7:J8"
        assert await google_sheets.append_rows(c, SID, "T", []) is None


async def test_read_and_update_range(service_account):
    seen = []

    def tail(req):
        seen.append((req.method, req.url.path, dict(req.url.params), req.content.decode() if req.content else ""))
        if req.method == "GET":
            return httpx.Response(200, json={"values": [["Date", "Profile"], ["2026-10-03", "Sam"]]})
        return httpx.Response(200, json={})

    async with mock(_sheets(tail)) as c:
        assert await google_sheets.read_range(c, SID, "'T'!A1:J") == [["Date", "Profile"], ["2026-10-03", "Sam"]]
        await google_sheets.update_range(c, SID, "'T'!F5:G5", [['=HYPERLINK("u","Download")', "Tailored"]])
    get, put = seen
    assert get[0] == "GET" and get[1].endswith("/values/'T'!A1:J")
    assert put[0] == "PUT" and put[1].endswith("/values/'T'!F5:G5") and put[2] == {"valueInputOption": "USER_ENTERED"}
    assert json.loads(put[3])["values"] == [['=HYPERLINK("u","Download")', "Tailored"]]


def _recorder(calls):
    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path.endswith(":batchUpdate"):
            return httpx.Response(200, json={"replies": [{"addSheet": {"properties": {"sheetId": 9, "title": "t"}}}]})
        return httpx.Response(200, json={})
    return handler


def _validations(calls):
    return [r["setDataValidation"] for _, _, body in calls if body and "requests" in body
            for r in body["requests"] if "setDataValidation" in r]


async def test_ensure_tab_puts_checkboxes_on_the_named_column(service_account):
    calls = []
    async with mock(_recorder(calls)) as c:
        await google_sheets.ensure_tab(c, SID, "t", ["Date", "Applied", "Location", "Checks"], {"tabs": {}}, checkbox="Applied")
    [v] = _validations(calls)
    assert v["range"]["startColumnIndex"] == 1 and v["range"]["endColumnIndex"] == 2


async def test_ensure_tab_without_checkboxes(service_account):
    calls = []
    async with mock(_recorder(calls)) as c:
        await google_sheets.ensure_tab(c, SID, "Checks", ["Sheet row", "Result"], {"tabs": {}})
    assert _validations(calls) == []


async def test_clear_range(service_account):
    calls = []
    async with mock(_recorder(calls)) as c:
        await google_sheets.clear_range(c, SID, "'Checks · a'!A2:Z")
    method, path, _ = calls[-1]
    assert method == "POST" and path.endswith(":clear") and "Checks" in path
