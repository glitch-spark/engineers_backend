"""Unit tests for failed-job retry field reset (no Mongo / no LLM)."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.routers.resume import _reset_failed_job_fields  # noqa: E402


def _failed_job(**overrides):
    base = {
        "status": "failed",
        "step": "generating_resume",
        "errorMessage": "502: style block",
        "startedAt": datetime.now(timezone.utc),
        "completedAt": datetime.now(timezone.utc),
        "executionMs": 1234,
        "pdfFilename": "Profile/Resume (Co).pdf",
        "pdfData": "abc",
        "s3Key": "resumes/x.pdf",
        "s3Url": "https://example/x.pdf",
        "plainText": "plain",
        "coverLetterText": "dear hiring",
        "screeningPairs": [{"question": "q", "answer": "a"}],
        "screeningQuestions": ["q1"],
        "inputTokens": 10,
        "outputTokens": 20,
        "reasoningTokens": 5,
        "resumeLlmProvider": "free",
        "resumeLlmModel": "m",
        "resumeLlmFallbackUsed": True,
        "resumeLlmFallbackReason": "timeout",
        "screeningLlmProvider": "openai",
        "screeningLlmModel": "gpt",
        "screeningLlmFallbackUsed": False,
        "screeningLlmFallbackReason": None,
        "generateCoverLetter": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_reset_failed_job_fields_queues_and_clears_outputs():
    job = _failed_job()
    _reset_failed_job_fields(job)

    assert job.status == "queued"
    assert job.step == "queued"
    assert job.errorMessage is None
    assert job.startedAt is None
    assert job.completedAt is None
    assert job.executionMs is None
    assert job.pdfData is None
    assert job.s3Key is None
    assert job.s3Url is None
    assert job.plainText is None
    assert job.coverLetterText is None
    assert job.screeningPairs == []
    assert job.inputTokens is None
    assert job.outputTokens is None
    assert job.reasoningTokens is None
    assert job.resumeLlmProvider is None
    assert job.resumeLlmModel is None
    assert job.resumeLlmFallbackUsed is None
    assert job.resumeLlmFallbackReason is None
    assert job.screeningLlmProvider is None
    assert job.screeningLlmModel is None
    assert job.screeningLlmFallbackUsed is None
    assert job.screeningLlmFallbackReason is None
    # Preserved for re-run
    assert job.pdfFilename == "Profile/Resume (Co).pdf"
    assert job.screeningQuestions == ["q1"]
    assert job.generateCoverLetter is True
