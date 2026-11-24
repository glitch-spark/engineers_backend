import os
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402

from app import resume_files  # noqa: E402
from app.caller_teams import files  # noqa: E402
from app.caller_teams.files import brief_filename, brief_pdf, round_files  # noqa: E402
from app.models.interview import InterviewCaller, InterviewResumeLink, InterviewStageEvent  # noqa: E402

ACC = "64b000000000000000000002"
JOB = "64b000000000000000000001"


def _iv(link=None, note="Bring <script>alert(1)</script>"):
    rounds = [
        InterviewStageEvent(id="r1", stage="intro", at=datetime(2026, 11, 1), scheduledAt=datetime(2026, 11, 2, 15), status="passed"),
        InterviewStageEvent(
            id="r2", stage="tech_round_1", at=datetime(2026, 11, 3), status="scheduled", note=note,
            scheduledAt=datetime(2026, 11, 24, 15, 30), endsAt=datetime(2026, 11, 24, 16, 30),
            caller=InterviewCaller(enabled=True, callerName="Secret Caller", method="video", methodValue="https://meet.example/x"),
        ),
    ]
    return SimpleNamespace(
        id=JOB, accountId=ACC, companyName="Acme/Inc", appliedPosition="Backend Dev", mainTechStack="Python",
        jobUrl="https://jobs.example/1", interviewerName="Secret Interviewer", stageHistory=rounds, resumeLink=link,
    )


@pytest.fixture
def pdf(monkeypatch):
    seen = {}

    async def fake(html, *a, **k):
        seen["html"] = html
        return b"%PDF"

    async def get(_id):
        return SimpleNamespace(name="Eric Owner", email="e@x.io", country="US", region=None)

    monkeypatch.setattr(files, "_html_to_pdf", fake)
    monkeypatch.setattr(files, "Account", SimpleNamespace(get=get))
    return seen


def test_brief_filename():
    assert brief_filename("Acme/Inc", "Tech round 1", "2026-11-24") == "Brief - Acme Inc - Tech round 1 - 2026-11-24.pdf"
    assert brief_filename("Acme", "Intro", None) == "Brief - Acme - Intro.pdf"


async def test_brief_html_has_fields_and_escapes(pdf):
    name, data = await brief_pdf(_iv(InterviewResumeLink(kind="none", jobDescription="Build APIs")), "r2", "America/Chicago")
    html = pdf["html"]
    assert data == b"%PDF" and name == "Brief - Acme Inc - Tech Round 1 - 2026-11-24.pdf"
    for text in ("Acme/Inc", "Tech Round 1", "Scheduled", "Backend Dev", "Python", "https://jobs.example/1", "Eric Owner",
                 "Video meeting", "https://meet.example/x", "Build APIs", "Nov 2, 2026", "Intro", "Passed"):
        assert text in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Secret Caller" not in html and "Secret Interviewer" not in html and "e@x.io" not in html


async def test_brief_when_in_team_timezone(pdf):
    await brief_pdf(_iv(), "r2", "America/Chicago")
    assert "Tue, Nov 24, 2026 · 9:30 – 10:30 AM CST" in pdf["html"]


def _stub_resume(monkeypatch, result):
    async def read(**kw):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(files.resume_files, "read_linked_resume", read)


async def test_round_files_tailored_resume(pdf, monkeypatch):
    _stub_resume(monkeypatch, ("Acme.pdf", b"%PDF-resume"))
    got, note = await round_files(_iv(InterviewResumeLink(kind="tailored", resumeJobId=JOB)), "r2", "UTC")
    assert [n for n, _ in got][1] == "Acme.pdf" and len(got) == 2 and note is None


async def test_round_files_missing_resume_keeps_brief(pdf, monkeypatch):
    _stub_resume(monkeypatch, resume_files.FileUnavailable("gone"))
    got, note = await round_files(_iv(InterviewResumeLink(kind="tailored", resumeJobId=JOB)), "r2", "UTC")
    assert len(got) == 1 and note == "No resume linked — ask the interview manager."


async def test_round_files_transient_resume_error_raises(pdf, monkeypatch):
    _stub_resume(monkeypatch, RuntimeError("s3 timeout"))
    with pytest.raises(RuntimeError):
        await round_files(_iv(InterviewResumeLink(kind="tailored", resumeJobId=JOB)), "r2", "UTC")


async def test_round_files_no_link(pdf, monkeypatch):
    _stub_resume(monkeypatch, None)
    got, note = await round_files(_iv(None), "r2", "UTC")
    assert len(got) == 1 and note == "No resume linked — ask the interview manager."


async def test_round_files_brief_failure_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("chromium")

    monkeypatch.setattr(files, "_html_to_pdf", boom)
    monkeypatch.setattr(files, "Account", SimpleNamespace(get=lambda _id: _async(None)))
    with pytest.raises(RuntimeError):
        await round_files(_iv(), "r2", "UTC")


async def _async(v):
    return v
