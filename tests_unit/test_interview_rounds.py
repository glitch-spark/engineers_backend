"""Unit tests for interview round counting (moved out of app.routers.metrics)."""

import os
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.interview_rounds import BREAKDOWN_ORDER, count_interview_rounds

START, END = datetime(2026, 9, 28), datetime(2026, 10, 4, 23, 59, 59)


def _iv(stage, status, scheduled_at, history=None):
    stage_history = [
        SimpleNamespace(stage=h_stage, scheduledAt=h_at) for h_stage, h_at in (history or [])
    ]
    return SimpleNamespace(
        stage=stage,
        status=status,
        scheduledAt=scheduled_at,
        stageHistory=stage_history,
    )


def test_counts_each_dated_round_by_group():
    iv = _iv("tech_round_2", "scheduled", datetime(2026, 10, 1, 15),
             history=[("intro", datetime(2026, 9, 28, 14)), ("tech_round_1", datetime(2026, 9, 30, 14))])
    rounds, canceled, bd = count_interview_rounds(iv, START, END)
    assert (rounds, canceled) == (3, 0)
    assert bd == {"intro": 1, "tech": 2, "hiring": 0, "panel": 0, "final": 0}


def test_excluded_stages_not_counted():
    iv = _iv("home_assessment", "scheduled", datetime(2026, 9, 29),
             history=[("ai_interview", datetime(2026, 9, 28))])
    assert count_interview_rounds(iv, START, END)[0] == 0


def test_canceled_round_counts_as_canceled():
    # Canceled is a round status now (the old stage-rejected + scheduled trick is migrated away).
    iv = _iv("intro", "canceled", datetime(2026, 9, 30))
    assert count_interview_rounds(iv, START, END)[:2] == (0, 1)


def test_rounds_outside_window_ignored():
    iv = _iv("final", "scheduled", datetime(2026, 10, 5, 9))
    assert count_interview_rounds(iv, START, END)[0] == 0


def test_breakdown_order_labels():
    assert [label for _, label in BREAKDOWN_ORDER] == ["Intro", "Tech", "Hiring", "Panel", "Final"]
