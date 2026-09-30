"""Unit tests for interview status/stage normalisation and migration rules (no database)."""

import copy
import os
from datetime import datetime, timezone

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.interview_normalize import (  # noqa: E402
    STAGE_VALUES,
    STATUS_VALUES,
    normalize_stage,
    normalize_status,
    resolve_interview,
)
from app.models.interview import InterviewStage, InterviewStageEvent, InterviewStatus  # noqa: E402

NOW = datetime(2026, 9, 30, 12)


def _entry(id_, stage, status=None, *, when=datetime(2026, 9, 1), note="", transcript=""):
    return {
        "id": id_,
        "stage": stage,
        "status": status,
        "at": when,
        "scheduledAt": when,
        "note": note,
        "transcript": transcript,
    }


def _iv(history, **top):
    doc = {
        "stage": history[-1]["stage"] if history else None,
        "status": history[-1]["status"] if history else None,
        "scheduledAt": history[-1]["scheduledAt"] if history else datetime(2026, 9, 1),
        "stageHistory": history,
    }
    doc.update(top)
    return doc


def test_normalize_status_maps_legacy():
    assert normalize_status("failed") == "rejected"
    assert normalize_status("no_show") == "canceled"
    assert normalize_status("rescheduled") == "scheduled"
    assert normalize_status("InterviewStatus.passed") == "passed"
    assert normalize_status("") is None
    assert normalize_status(None) is None


def test_normalize_stage_maps_legacy():
    assert normalize_stage("tech") == "tech_round_1"
    assert normalize_stage("tech1") == "tech_round_1"
    assert normalize_stage("tech2") == "tech_round_2"
    assert normalize_stage("hiring_manager") == "cultural"
    assert normalize_stage("offer") == "final"
    assert normalize_stage("others") == "intro"
    assert normalize_stage("InterviewStage.panel") == "panel"
    assert normalize_stage("rejected") == "rejected"


def test_rejected_tip_scheduled_becomes_canceled_on_previous():
    raw = _iv([_entry("a", "intro", "completed"), _entry("b", "rejected", "scheduled")])
    res = resolve_interview(raw, now=NOW)
    assert len(res.doc["stageHistory"]) == 1
    assert res.doc["stageHistory"][0]["status"] == "canceled"
    assert res.doc["stage"] == "intro"
    assert res.doc["status"] == "canceled"
    assert res.question_remap == {"b": "a"}
    assert res.rules["rejected_to_canceled"] == 1


def test_rejected_tip_completed_becomes_rejected_on_previous():
    raw = _iv([_entry("a", "intro", "completed"), _entry("b", "rejected", "completed")])
    res = resolve_interview(raw, now=NOW)
    assert res.doc["stageHistory"][0]["status"] == "rejected"
    assert res.doc["status"] == "rejected"
    assert res.rules["rejected_to_rejected"] == 1


def test_rejected_tip_failed_maps_then_rejects():
    raw = _iv([_entry("a", "intro", "completed"), _entry("b", "rejected", "failed")])
    res = resolve_interview(raw, now=NOW)
    assert res.doc["stageHistory"][0]["status"] == "rejected"


def test_rejected_tip_uses_top_status_when_tip_status_none():
    raw = _iv(
        [_entry("a", "intro", "completed"), _entry("b", "rejected", None)],
        status="scheduled",
    )
    res = resolve_interview(raw, now=NOW)
    assert res.doc["stageHistory"][0]["status"] == "canceled"


def test_rejected_merges_note_and_transcript():
    raw = _iv([
        _entry("a", "intro", "completed", transcript="A"),
        _entry("b", "rejected", "completed", note="N", transcript="B"),
    ])
    prev = resolve_interview(raw, now=NOW).doc["stageHistory"][0]
    assert prev["note"] == "N"
    assert prev["transcript"] == "A\n\n---\nB"

    raw = _iv([
        _entry("a", "intro", "completed", note="keep", transcript="A"),
        _entry("b", "rejected", "completed", note="   ", transcript="\n"),
    ])
    prev = resolve_interview(raw, now=NOW).doc["stageHistory"][0]
    assert prev["note"] == "keep"
    assert prev["transcript"] == "A"


def test_rejected_only_history_becomes_intro():
    raw = _iv([_entry("b", "rejected", "completed")])
    res = resolve_interview(raw, now=NOW)
    assert res.doc["stageHistory"][0]["stage"] == "intro"
    assert res.doc["stageHistory"][0]["status"] == "rejected"
    assert res.doc["stage"] == "intro"
    assert res.question_remap == {}
    assert res.rules["rejected_only_to_intro"] == 1


def test_empty_history_top_level_rejected_becomes_intro():
    raw = {"stage": "rejected", "status": "scheduled", "scheduledAt": datetime(2026, 9, 1), "stageHistory": []}
    res = resolve_interview(raw, now=NOW)
    assert res.doc["stage"] == "intro"
    assert res.doc["status"] == "canceled"
    assert res.rules["rejected_only_to_intro"] == 1


def test_earlier_none_becomes_passed_latest_untouched():
    raw = _iv([_entry("a", "intro", None), _entry("b", "tech_round_1", "completed")])
    res = resolve_interview(raw, now=NOW)
    assert [e["status"] for e in res.doc["stageHistory"]] == ["passed", "completed"]
    assert res.rules["earlier_none_to_passed"] == 1


def test_latest_none_past_completed_future_scheduled():
    past = _iv([_entry("a", "intro", None, when=datetime(2026, 9, 29))])
    assert resolve_interview(past, now=NOW).doc["status"] == "completed"
    future = _iv([_entry("a", "intro", None, when=datetime(2026, 10, 2))])
    assert resolve_interview(future, now=NOW).doc["status"] == "scheduled"


def test_latest_none_with_aware_datetime():
    raw = _iv([_entry("a", "intro", None, when=datetime(2026, 10, 2, tzinfo=timezone.utc))])
    res = resolve_interview(raw, now=datetime(2026, 9, 30, 12, tzinfo=timezone.utc))
    assert res.doc["stageHistory"][0]["status"] == "scheduled"


def test_remap_only_contains_removed_ids():
    raw = _iv([
        _entry("a", "intro", "passed"),
        _entry("c", "tech_round_1", "completed"),
        _entry("b", "rejected", "completed"),
    ])
    assert resolve_interview(raw, now=NOW).question_remap == {"b": "c"}

    raw = _iv([_entry("a", "intro", "completed"), _entry(None, "rejected", "completed")])
    assert resolve_interview(raw, now=NOW).question_remap == {}


def test_legacy_values_mapped_in_history_and_top():
    raw = _iv([_entry("a", "tech2", "no_show")])
    res = resolve_interview(raw, now=NOW)
    assert res.doc["stageHistory"][0]["stage"] == "tech_round_2"
    assert res.doc["stageHistory"][0]["status"] == "canceled"
    assert res.doc["stage"] == "tech_round_2"
    assert res.doc["status"] == "canceled"
    assert res.rules["legacy_value_mapped"] >= 2


def test_resolve_is_idempotent():
    raw = _iv([
        _entry("a", "intro", None),
        _entry("c", "tech1", "failed", transcript="x"),
        _entry("b", "rejected", "scheduled", note="n"),
    ])
    r1 = resolve_interview(raw, now=NOW)
    r2 = resolve_interview(r1.doc, now=NOW)
    assert r2.doc == r1.doc
    assert r2.question_remap == {}
    assert sum(r2.rules.values()) == 0


def test_resolve_does_not_mutate_input():
    raw = _iv([_entry("a", "intro", None), _entry("b", "rejected", "completed", note="n")])
    before = copy.deepcopy(raw)
    resolve_interview(raw, now=NOW)
    assert raw == before


def test_stage_event_accepts_legacy_status():
    assert InterviewStageEvent(stage="intro", at=NOW, status="no_show").status == InterviewStatus.canceled
    assert InterviewStageEvent(stage="intro", at=NOW, status="failed").status == InterviewStatus.rejected
    assert InterviewStageEvent(stage="intro", at=NOW, status="rescheduled").status == InterviewStatus.scheduled


def test_enums_match_spec():
    assert [s.value for s in InterviewStatus] == list(STATUS_VALUES)
    assert [s.value for s in InterviewStage] == list(STAGE_VALUES)


def test_translate_rejected_scheduled_is_canceled():
    from app.interview_normalize import translate_legacy_write
    assert translate_legacy_write("rejected", "scheduled") == (None, "canceled")


def test_translate_rejected_other_is_rejected():
    from app.interview_normalize import translate_legacy_write
    assert translate_legacy_write("rejected", "completed") == (None, "rejected")
    assert translate_legacy_write("rejected", None) == (None, "rejected")


def test_translate_maps_legacy_pair():
    from app.interview_normalize import translate_legacy_write
    assert translate_legacy_write("tech2", "no_show") == ("tech_round_2", "canceled")


def test_translate_passes_empty_strings():
    from app.interview_normalize import translate_legacy_write
    assert translate_legacy_write("", "") == ("", "")
    assert translate_legacy_write(None, None) == (None, None)


def test_changed_fields_only_diff():
    from scripts.migrate_interview_statuses import changed_fields
    raw = _iv([_entry("a", "intro", "completed")], companyName="Acme")
    assert changed_fields(raw, copy.deepcopy(raw)) == {}
    doc = copy.deepcopy(raw)
    doc["status"] = "rejected"
    doc["stageHistory"][0]["status"] = "rejected"
    doc["companyName"] = "ignored"
    assert set(changed_fields(raw, doc)) == {"status", "stageHistory"}


def test_question_updates_follow_remap_and_round_stage():
    from scripts.migrate_interview_statuses import question_updates
    iv = "iv1"
    questions = [
        {"_id": 1, "interviewId": iv, "stageId": "b", "stage": "rejected"},   # removed round -> prev
        {"_id": 2, "interviewId": iv, "stageId": "a", "stage": "intro"},      # already right
        {"_id": 3, "interviewId": iv, "stageId": "c", "stage": "tech"},       # legacy stage on surviving round
        {"_id": 4, "interviewId": iv, "stageId": None, "stage": "tech"},      # no round link: map value only
        {"_id": 5, "interviewId": "other", "stageId": "b", "stage": "rejected"},  # other interview: untouched
        {"_id": 6, "interviewId": iv, "stageId": "d", "stage": "panel"},      # snapshot differs, not legacy: keep
    ]
    remap = {(iv, "b"): "a"}
    stage_by_round = {(iv, "a"): "intro", (iv, "c"): "tech_round_1", (iv, "d"): "final"}
    assert dict(question_updates(questions, remap, stage_by_round)) == {
        1: {"stageId": "a", "stage": "intro"},
        3: {"stage": "tech_round_1"},
        4: {"stage": "tech_round_1"},
    }


def test_plan_interview_returns_payload_and_question_updates():
    from scripts.migrate_interview_statuses import plan_interview
    raw = _iv([_entry("a", "intro", "completed"), _entry("b", "rejected", "scheduled")])
    raw["_id"] = "iv1"
    questions = [{"_id": 1, "interviewId": "iv1", "stageId": "b", "stage": "rejected"}]
    payload, q_updates, res = plan_interview(raw, questions, now=NOW)
    assert payload["status"] == "canceled"
    assert q_updates == [(1, {"stageId": "a", "stage": "intro"})]
    assert res.rules["rejected_to_canceled"] == 1


def test_resync_moves_ends_at_with_scheduled_at():
    from datetime import timedelta
    raw = _iv(
        [_entry("a", "intro", "completed", when=datetime(2026, 9, 1, 10)),
         _entry("b", "rejected", "completed", when=datetime(2026, 9, 15, 10))],
    )
    raw["scheduledAt"] = datetime(2026, 9, 15, 10)
    raw["endsAt"] = datetime(2026, 9, 15, 10, 45)
    doc = resolve_interview(raw, now=NOW).doc
    assert doc["scheduledAt"] == datetime(2026, 9, 1, 10)
    assert doc["endsAt"] == datetime(2026, 9, 1, 10, 45)  # duration kept

    raw["endsAt"] = datetime(2026, 9, 20)  # nonsense duration -> 1 hour
    assert resolve_interview(raw, now=NOW).doc["endsAt"] == datetime(2026, 9, 1, 10) + timedelta(hours=1)


def test_changed_fields_includes_ends_at():
    from scripts.migrate_interview_statuses import changed_fields
    raw = _iv([_entry("a", "intro", "completed")])
    doc = copy.deepcopy(raw)
    doc["endsAt"] = datetime(2026, 9, 1, 11)
    assert set(changed_fields(raw, doc)) == {"endsAt"}


def test_new_round_does_not_inherit_previous_outcome():
    from app.routers.interviews import _carry_status
    assert _carry_status(InterviewStatus.canceled, appended=True) == InterviewStatus.scheduled
    assert _carry_status(InterviewStatus.rejected, appended=True) == InterviewStatus.scheduled
    assert _carry_status(InterviewStatus.canceled, appended=False) == InterviewStatus.canceled
