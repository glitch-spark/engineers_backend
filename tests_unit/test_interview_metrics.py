"""Unit tests for status-based interview round counting (no database)."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.interview_normalize import interview_status_value  # noqa: E402
from app.models.interview import InterviewStatus  # noqa: E402
from app.routers.applications import pipeline_outcome_for_status  # noqa: E402
from app.interview_rounds import count_interview_rounds as _count_interview_rounds  # noqa: E402

START = datetime(2026, 9, 28, tzinfo=timezone.utc)
END = datetime(2026, 10, 4, 23, 59, 59, tzinfo=timezone.utc)


def _round(stage, when, status):
    return SimpleNamespace(stage=stage, scheduledAt=when, status=status)


def _iv(*rounds):
    tip = rounds[-1]
    return SimpleNamespace(
        stage=tip.stage, status=tip.status, scheduledAt=tip.scheduledAt, stageHistory=list(rounds),
    )


def test_canceled_round_counts_as_canceled_not_held():
    iv = _iv(
        _round("intro", datetime(2026, 9, 29), "passed"),
        _round("tech_round_1", datetime(2026, 10, 1), "canceled"),
    )
    held, canceled, breakdown = _count_interview_rounds(iv, START, END)
    assert (held, canceled) == (1, 1)
    assert breakdown["intro"] == 1
    assert breakdown["tech"] == 0


def test_earlier_rounds_of_canceled_interview_now_count():
    # Old code: stage rejected + scheduled counted 0 held rounds for the whole interview.
    iv = _iv(
        _round("intro", datetime(2026, 9, 29), "passed"),
        _round("final", datetime(2026, 9, 30), "canceled"),
    )
    held, _, _ = _count_interview_rounds(iv, START, END)
    assert held == 1


def test_ai_and_home_assessment_excluded():
    iv = _iv(
        _round("ai_interview", datetime(2026, 9, 29), "passed"),
        _round("home_assessment", datetime(2026, 9, 30), "passed"),
        _round("panel", datetime(2026, 10, 1), "completed"),
    )
    held, canceled, breakdown = _count_interview_rounds(iv, START, END)
    assert (held, canceled) == (1, 0)
    assert breakdown["panel"] == 1


def test_out_of_range_canceled_not_counted():
    iv = _iv(_round("intro", datetime(2026, 9, 20), "canceled"))
    assert _count_interview_rounds(iv, START, END)[:2] == (0, 0)


def test_dashboard_status_is_plain_string():
    assert interview_status_value(InterviewStatus.passed) == "passed"
    assert interview_status_value("failed") == "rejected"
    assert interview_status_value(None) is None


def test_pipeline_outcome_for_status():
    assert pipeline_outcome_for_status(InterviewStatus.rejected) == "rejected"
    assert pipeline_outcome_for_status("rejected") == "rejected"
    assert pipeline_outcome_for_status(InterviewStatus.canceled) is None
    assert pipeline_outcome_for_status("passed") is None
    assert pipeline_outcome_for_status(None) is None
