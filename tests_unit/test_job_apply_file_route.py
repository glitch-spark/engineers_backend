"""The sheet's download link end to end through the real route, with the database and S3 stubbed (no network):
`GET /job-applies/files/<token>` answers `<Company>.zip` holding the resume."""
import base64
import io
import os
import zipfile
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import resume_files  # noqa: E402
from app.job_apply import file_links  # noqa: E402
from app.main import app  # noqa: E402

JOB_ID, ACC_ID, RESUME_ID = "64b000000000000000000001", "64b000000000000000000002", "64b000000000000000000003"
PDF = b"%PDF-1.7 tailored"


class _Jobs:
    doc: dict | None = None

    @classmethod
    def get_motor_collection(cls):
        async def find_one(query, projection=None):
            return cls.doc

        return SimpleNamespace(find_one=find_one)


class _Accounts:
    resumes: list = []

    @classmethod
    async def get(cls, _id):
        return SimpleNamespace(resumes=cls.resumes)


@pytest.fixture
def stubs(monkeypatch):
    _Jobs.doc = {"companyName": "Acme, Inc.", "pdfData": base64.b64encode(PDF).decode()}
    _Accounts.resumes = [SimpleNamespace(id=RESUME_ID, filename="Eric FINAL.pdf", s3Key="profiles/e/r.pdf")]
    stored = {"profiles/e/r.pdf": b"%PDF-1.7 uploaded", "jobs/t.pdf": b"%PDF-1.7 tailored in s3"}

    async def read(key):
        if key not in stored:
            raise resume_files.FileUnavailable(key)
        return stored[key]

    monkeypatch.setattr(resume_files, "ResumeApplication", _Jobs)
    monkeypatch.setattr(resume_files, "Account", _Accounts)
    monkeypatch.setattr(resume_files, "read_resume_file", read)
    return stored


async def _get(url: str) -> httpx.Response:
    path = url[url.index("/job-applies/files/"):]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)  # no Authorization header: the link is the permission


def _only_entry(res: httpx.Response) -> tuple[str, bytes]:
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(res.content))
    assert len(z.namelist()) == 1 and z.testzip() is None
    return z.namelist()[0], z.read(z.namelist()[0])


async def test_tailored_resume_downloads_as_a_zip_named_after_its_company(stubs):
    res = await _get(file_links.file_url("tailored", JOB_ID))
    assert _only_entry(res) == ("resume.pdf", PDF)
    assert res.headers["content-disposition"] == "attachment; filename=\"Acme_ Inc.zip\"; filename*=UTF-8''Acme_%20Inc.zip"


async def test_tailored_resume_stored_in_s3(stubs):
    _Jobs.doc = {"companyName": "Café Ünï", "s3Key": "jobs/t.pdf"}
    res = await _get(file_links.file_url("tailored", JOB_ID))
    assert _only_entry(res) == ("resume.pdf", b"%PDF-1.7 tailored in s3")
    res.headers["content-disposition"].encode("latin-1")  # the header is valid even for an accented name
    assert "filename*=UTF-8''Caf%C3%A9%20%C3%9Cn%C3%AF.zip" in res.headers["content-disposition"]


async def test_uploaded_resume_is_named_by_the_company_in_its_link(stubs):
    res = await _get(file_links.file_url("resume", RESUME_ID, ACC_ID, company="Globex"))
    assert _only_entry(res) == ("resume.pdf", b"%PDF-1.7 uploaded")
    assert res.headers["content-disposition"].startswith('attachment; filename="Globex.zip"')


async def test_link_made_before_company_names_still_downloads(stubs):
    res = await _get(file_links.file_url("resume", RESUME_ID, ACC_ID))
    assert _only_entry(res)[0] == "resume.pdf"
    assert res.headers["content-disposition"].startswith('attachment; filename="Resume.zip"')


async def test_uploaded_text_resume_keeps_its_type(stubs):
    _Accounts.resumes = [SimpleNamespace(id=RESUME_ID, filename="cv.TXT", s3Key="profiles/e/r.pdf")]
    res = await _get(file_links.file_url("resume", RESUME_ID, ACC_ID, company="Globex"))
    assert _only_entry(res)[0] == "resume.txt"


async def test_missing_things_are_404_not_500(stubs):
    assert (await _get("/job-applies/files/not-a-token")).status_code == 404
    _Jobs.doc = None
    assert (await _get(file_links.file_url("tailored", JOB_ID))).status_code == 404  # resume deleted
    _Jobs.doc = {"companyName": "Acme"}
    assert (await _get(file_links.file_url("tailored", JOB_ID))).status_code == 404  # not generated yet
    _Jobs.doc = {"companyName": "Acme", "s3Key": "jobs/gone.pdf"}
    assert (await _get(file_links.file_url("tailored", JOB_ID))).status_code == 404  # file removed from storage
    _Accounts.resumes = [SimpleNamespace(id=RESUME_ID, filename="x.pdf", s3Key=None)]
    assert (await _get(file_links.file_url("resume", RESUME_ID, ACC_ID))).status_code == 404  # original never stored


async def test_storage_outage_asks_to_retry(stubs, monkeypatch):
    async def boom(key):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(resume_files, "read_resume_file", boom)
    res = await _get(file_links.file_url("resume", RESUME_ID, ACC_ID))
    assert res.status_code == 502 and res.headers["content-type"] != "application/zip"
