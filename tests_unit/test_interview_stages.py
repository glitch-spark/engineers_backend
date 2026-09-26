"""Unit tests for per-stage interview helpers (no database)."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from fastapi import HTTPException  # noqa: E402

from app.models.interview import InterviewStage, InterviewStageEvent, InterviewStatus  # noqa: E402
from app.routers.interviews import (  # noqa: E402
    StageHistoryItemIn,
    _reject_adjacent_duplicate,
    _set_stage_history,
    _sync_top_from_tip,
    migrate_legacy_stage_fields,
)

AT = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)


def _iv(**kw):
    base = dict(
        stage=None,
        status=None,
        scheduledAt=datetime(2026, 4, 25, 14, 30),
        endsAt=None,
        createdAt=AT,
        createdBy=None,
        transcript="",
        note="",
        stageHistory=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_legacy_entry_id_is_stable_across_parses():
    raw = {"stage": "intro", "at": AT, "scheduledAt": None}
    assert InterviewStageEvent.model_validate(raw).id == InterviewStageEvent.model_validate(raw).id


def test_explicit_id_is_kept():
    e = InterviewStageEvent(id="abc", stage="intro", at=AT)
    assert e.id == "abc"


def test_migrate_seeds_history_and_moves_fields():
    iv = _iv(stage=InterviewStage.intro, status=InterviewStatus.completed, transcript="t", note="n")
    assert migrate_legacy_stage_fields(iv) is True
    tip = iv.stageHistory[-1]
    assert (tip.stage, tip.status, tip.transcript, tip.note) == ("intro", InterviewStatus.completed, "t", "n")
    assert iv.transcript == "" and iv.note == ""
    assert migrate_legacy_stage_fields(iv) is False


def test_migrate_does_not_overwrite_existing_tip_transcript():
    tip = InterviewStageEvent(stage="intro", at=AT, transcript="keep")
    iv = _iv(stage=InterviewStage.intro, transcript="legacy", stageHistory=[tip])
    migrate_legacy_stage_fields(iv)
    assert iv.stageHistory[-1].transcript == "keep"
    assert iv.transcript == ""


def test_set_stage_history_merges_by_id_and_reports_removed():
    a = InterviewStageEvent(id="a", stage="intro", at=AT, transcript="A", note="a")
    b = InterviewStageEvent(id="b", stage="tech_round_1", at=AT, transcript="B")
    c = InterviewStageEvent(id="c", stage="final", at=AT, note="C")
    iv = _iv(stageHistory=[a, b, c])
    removed = _set_stage_history(
        iv,
        [StageHistoryItemIn(id="a", stage="intro"), StageHistoryItemIn(id="c", stage="final", scheduledAt="2026-05-10")],
        by=None,
    )
    assert removed == {"b"}
    assert [e.id for e in iv.stageHistory] == ["a", "c"]
    assert iv.stageHistory[0].transcript == "A"
    assert iv.stageHistory[1].note == "C"
    assert iv.stageHistory[1].scheduledAt.date().isoformat() == "2026-05-10"


def test_set_stage_history_falls_back_to_stage_name():
    a = InterviewStageEvent(id="a", stage="intro", at=AT, transcript="A")
    iv = _iv(stageHistory=[a])
    _set_stage_history(iv, [StageHistoryItemIn(stage="intro"), StageHistoryItemIn(stage="panel")], by=None)
    assert iv.stageHistory[0].id == "a"
    assert iv.stageHistory[0].transcript == "A"
    assert iv.stageHistory[1].stage == "panel"


def test_sync_top_from_tip_mirrors_stage_status_and_date():
    tip = InterviewStageEvent(
        stage="panel", at=AT, status=InterviewStatus.passed, scheduledAt=datetime(2026, 5, 3, 12, 0),
    )
    iv = _iv(stageHistory=[tip])
    _sync_top_from_tip(iv)
    assert iv.stage == InterviewStage.panel
    assert iv.status == InterviewStatus.passed
    assert iv.scheduledAt.date().isoformat() == "2026-05-03"
    assert iv.endsAt > iv.scheduledAt


def test_sync_keeps_time_when_same_calendar_day():
    tip = InterviewStageEvent(stage="panel", at=AT, scheduledAt=datetime(2026, 4, 25, 12, 0))
    iv = _iv(stageHistory=[tip])
    _sync_top_from_tip(iv)
    assert iv.scheduledAt == datetime(2026, 4, 25, 14, 30)


def test_reject_adjacent_duplicate():
    iv = _iv(stageHistory=[
        InterviewStageEvent(stage="intro", at=AT),
        InterviewStageEvent(stage="intro", at=AT, scheduledAt=AT),
    ])
    with pytest.raises(HTTPException):
        _reject_adjacent_duplicate(iv)
