"""Retention of Job Applies data: runs and rows expire through a MongoDB TTL index (no database)."""
import os
from datetime import datetime, timedelta

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.models.job_apply import RETENTION_DAYS, JobApplyRow, JobApplyRun  # noqa: E402


def _ttl(model):
    return [ix.document for ix in model.Settings.indexes if "expireAfterSeconds" in ix.document]


def test_runs_and_rows_expire_after_retention():
    assert RETENTION_DAYS == 60
    for model in (JobApplyRun, JobApplyRow):
        expires = model.model_fields["expiresAt"].default_factory()
        assert abs((expires - datetime.utcnow()) - timedelta(days=RETENTION_DAYS)) < timedelta(minutes=1)
    for model in (JobApplyRun, JobApplyRow):
        (ttl,) = _ttl(model)
        assert list(ttl["key"]) == ["expiresAt"] and ttl["expireAfterSeconds"] == 0
