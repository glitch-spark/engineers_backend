"""Retention of Job Applies data (no database): runs are short-lived worklists; descriptions are cached per
URL and LLM readings per description for 30 days; the applied log is permanent."""
import os
from datetime import datetime, timedelta

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.models.job_apply import (  # noqa: E402
    CACHE_DAYS,
    RUN_RETENTION_DAYS,
    JobApplicationLog,
    JobApplyRow,
    JobApplyRun,
    JobExtractionCache,
    JobPostingCache,
)


def _ttl(model):
    return [ix.document for ix in model.Settings.indexes if "expireAfterSeconds" in ix.document]


def test_runs_and_rows_expire_after_seven_days():
    assert RUN_RETENTION_DAYS == 7
    for model in (JobApplyRun, JobApplyRow):
        expires = model.model_fields["expiresAt"].default_factory()
        assert abs((expires - datetime.utcnow()) - timedelta(days=RUN_RETENTION_DAYS)) < timedelta(minutes=1)
        (ttl,) = _ttl(model)
        assert list(ttl["key"]) == ["expiresAt"] and ttl["expireAfterSeconds"] == 0


def test_description_and_extraction_caches_last_30_days():
    assert CACHE_DAYS == 30
    (posting,) = _ttl(JobPostingCache)
    assert list(posting["key"]) == ["fetchedAt"] and posting["expireAfterSeconds"] == 30 * 24 * 3600
    (extraction,) = _ttl(JobExtractionCache)
    assert list(extraction["key"]) == ["createdAt"] and extraction["expireAfterSeconds"] == 30 * 24 * 3600


def test_applied_log_is_permanent_and_unique_per_file():
    assert _ttl(JobApplicationLog) == []
    unique = [ix.document for ix in JobApplicationLog.Settings.indexes if ix.document.get("unique")]
    assert [list(ix["key"]) for ix in unique] == [["userId", "url", "fileKey"]]  # resumeId or tailored:<jobId>


def test_screening_defaults():
    from typing import get_args

    from app.models.job_apply import RowStatus, RunStatus

    run, row = JobApplyRun.model_fields, JobApplyRow.model_fields
    assert run["phase"].default == "screen" and run["autoStart"].default is False and run["screenedAt"].default is None
    assert run["assignments"].default_factory() == {} and run["selection"].default_factory() == []
    assert row["screen"].default is None and row["groupKey"].default is None and row["forceInclude"].default is False
    assert "screened" in get_args(RunStatus) and "unassigned" in get_args(RowStatus)
