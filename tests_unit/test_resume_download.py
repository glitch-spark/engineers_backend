"""Resume page downloads (in-memory MongoDB via mongomock-motor): folder and zip names that aren't Latin-1 (curly
apostrophes, dashes, CJK) still download, since response headers carry them encoded."""

import io
import os
import zipfile
from urllib.parse import unquote

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, ResumeApplication, UserExtra
from app.routers import resume
from app.routers.resume import download_job

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["resume-download-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Account, ResumeApplication])

    async def fake_pdf(job):
        return b"%PDF-fake"

    monkeypatch.setattr(resume, "_fetch_pdf_bytes", fake_pdf)
    return database


async def _job(profile: str, company: str):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    acc = await Account(name=profile, createdBy=owner.id).insert()
    job = await ResumeApplication(
        userId=owner.id, accountId=acc.id, profileName=profile, companyName=company, companyNameLower=company.lower(),
        jobDescription="jd", status="completed",
    ).insert()
    return owner, job


@pytest.mark.parametrize("company", ["Macy’s", "Acme — Labs", "株式会社", "Société Générale"])
async def test_pdf_download_sends_the_folder_encoded(db, company):
    owner, job = await _job("Zoë", company)

    res = await download_job(str(job.id), owner, format="pdf")

    assert res.headers["X-Folder"].isascii()
    assert unquote(res.headers["X-Folder"]) == f"Zoë/{company}"


async def test_plain_folder_header_is_unchanged(db):
    owner, job = await _job("Alex Smith", "AT&T (Mobile), R&D")

    res = await download_job(str(job.id), owner, format="pdf")

    assert res.headers["X-Folder"] == "Alex Smith/AT&T (Mobile), R&D"


async def test_zip_download_names_the_zip_after_the_company(db):
    owner, job = await _job("Alex", "Macy’s / Inc")

    res = await download_job(str(job.id), owner)

    cd = res.headers["Content-Disposition"]
    assert cd.isascii()
    assert "filename*=UTF-8''" + "Macy%E2%80%99s%20_%20Inc.zip" in cd
    with zipfile.ZipFile(io.BytesIO(res.body)) as zf:
        assert zf.namelist() == ["Alex/Macy’s _ Inc/Resume.pdf"]
