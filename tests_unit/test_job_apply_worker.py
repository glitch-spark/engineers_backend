"""Unit tests for the worker's pure stage-selection helpers (no database)."""
import os
from datetime import date

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.job_apply.worker import failing_reasons, needs_stage, posted_for_row  # noqa: E402
from app.job_apply.schema import GateResult  # noqa: E402


def test_needs_stage_idempotent():
    assert needs_stage("pending", has_jd=False, scored=False, stage="fetch")
    assert not needs_stage("pending", has_jd=True, scored=False, stage="fetch")
    assert needs_stage("pending", has_jd=True, scored=False, stage="extract")
    assert needs_stage("fetched", has_jd=True, scored=False, stage="extract")
    assert not needs_stage("extracted", has_jd=True, scored=False, stage="extract")
    assert needs_stage("extracted", has_jd=True, scored=False, stage="score")
    assert needs_stage("excluded", has_jd=True, scored=False, stage="score")
    assert not needs_stage("excluded", has_jd=True, scored=True, stage="score")
    assert not needs_stage("scored", has_jd=True, scored=True, stage="score")
    for failed in ("fetch_failed", "llm_failed"):
        assert not any(needs_stage(failed, has_jd=False, scored=False, stage=s) for s in ("fetch", "extract", "score"))


def test_posted_priority():
    assert posted_for_row(date(2026, 9, 1), date(2026, 9, 20)) == date(2026, 9, 1)
    assert posted_for_row(None, date(2026, 9, 20)) == date(2026, 9, 20)
    assert posted_for_row(None, None) is None


def test_failing_reasons():
    gates = [GateResult(name="remote", result="fail", reason="hybrid"), GateResult(name="freshness", result="unknown", reason="date unknown")]
    assert failing_reasons(gates) == "remote: hybrid"
