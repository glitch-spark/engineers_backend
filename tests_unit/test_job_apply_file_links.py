"""Signed, login-free download links for resumes in the exported Google Sheet, and the zip they download."""
import io
import os
import zipfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402

from app.job_apply import file_links  # noqa: E402


def test_round_trip_and_url():
    token = file_links.make_token("tailored", "job123")
    assert file_links.read_token(token) == {"kind": "tailored", "id": "job123", "accountId": None, "company": None}
    url = file_links.file_url("resume", "r1", account_id="a1")
    assert "/job-applies/files/" in url
    assert file_links.read_token(url.rsplit("/", 1)[1]) == {"kind": "resume", "id": "r1", "accountId": "a1", "company": None}


def test_link_carries_the_company_for_the_zip_name():
    url = file_links.file_url("resume", "r1", account_id="a1", company="Acme Corp")
    assert file_links.read_token(url.rsplit("/", 1)[1])["company"] == "Acme Corp"
    assert file_links.read_token(file_links.make_token("resume", "r1", "a1", company=""))["company"] is None
    long = file_links.read_token(file_links.make_token("resume", "r1", "a1", company="x" * 500))["company"]
    assert long == "x" * file_links.MAX_NAME  # a link can't be made huge by a long company name


def test_rejects_tampered_and_expired():
    token = file_links.make_token("resume", "r1", account_id="a1")
    with pytest.raises(file_links.BadLink):
        file_links.read_token(token[:-2] + ("aa" if not token.endswith("aa") else "bb"))
    old = file_links.make_token("resume", "r1", account_id="a1", now=datetime.now(timezone.utc) - timedelta(days=31))
    with pytest.raises(file_links.BadLink):
        file_links.read_token(old)
    with pytest.raises(file_links.BadLink):
        from app.security import create_access_token
        file_links.read_token(create_access_token(sub="u", role="staff"))  # a login token is not a file link


def test_zip_name_is_the_company():
    assert file_links.zip_name("Acme Corp") == "Acme Corp.zip"
    assert file_links.zip_name("Acme, Inc. (EU)") == "Acme_ Inc. (EU).zip"
    assert file_links.zip_name("a/b\\c:d*e?") == "a_b_c_d_e_.zip"  # nothing a file system or header would choke on
    assert file_links.zip_name("  Acme Corp. ") == "Acme Corp.zip"  # no leading / trailing space or dot (Windows)
    assert file_links.zip_name("Café Ünï") == "Café Ünï.zip"  # letters of any language stay
    assert file_links.zip_name("") == file_links.zip_name(None) == file_links.zip_name("...") == "Resume.zip"
    assert len(file_links.zip_name("x" * 500)) == file_links.MAX_NAME + len(".zip")


def test_entry_name_keeps_the_file_type():
    assert file_links.entry_name("eric.pdf") == "resume.pdf"
    assert file_links.entry_name("Eric Resume FINAL.PDF") == "resume.pdf"
    assert file_links.entry_name("cv.txt") == "resume.txt" and file_links.entry_name("cv.md") == "resume.md"
    assert file_links.entry_name("noextension") == "resume.pdf" and file_links.entry_name(None) == "resume.pdf"
    assert file_links.entry_name("weird.exe") == "resume.pdf"  # only the types a profile can upload are kept


def test_build_zip_holds_one_file():
    data = b"%PDF-1.7 not really a pdf"
    with zipfile.ZipFile(io.BytesIO(file_links.build_zip(data, "resume.pdf"))) as z:
        assert z.namelist() == ["resume.pdf"]
        assert z.read("resume.pdf") == data
        assert z.testzip() is None


def test_download_header_is_plain_ascii_with_the_real_name_alongside():
    h = file_links.content_disposition("Café Ünï.zip")
    h.encode("latin-1")  # a header value must be encodable, or the response fails
    assert h.startswith("attachment;") and 'filename="Cafe Uni.zip"' in h
    assert "filename*=UTF-8''Caf%C3%A9%20%C3%9Cn%C3%AF.zip" in h
    assert file_links.content_disposition("Acme Corp.zip") == (
        "attachment; filename=\"Acme Corp.zip\"; filename*=UTF-8''Acme%20Corp.zip"
    )
    assert 'filename="download.zip"' in file_links.content_disposition("株式会社.zip")  # nothing ASCII to fall back on
