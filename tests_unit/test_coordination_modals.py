"""The three coordination forms: pure views, submission parsing and validation. Nothing here reaches Slack or a DB."""

import json
import os
from datetime import datetime, timezone

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402

from app.coordination import modals  # noqa: E402
from app.coordination.modals import (  # noqa: E402
    Origin,
    cancel_view,
    read_submission,
    reject_view,
    reschedule_view,
    submission_errors,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def submitted(callback_id, *, tz="America/Chicago", date=None, time=None, note=None, metadata=None):
    origin = Origin("IV:R", "C1", "200.2", tz)
    values = {"note": {"note_input": {"type": "plain_text_input", "value": note}}}
    if callback_id == "coord_reschedule":
        values["date"] = {"date_input": {"type": "datepicker", "selected_date": date}}
        values["time"] = {"time_input": {"type": "timepicker", "selected_time": time}}
    return {
        "callback_id": callback_id,
        "private_metadata": origin.to_metadata() if metadata is None else metadata,
        "state": {"values": values},
    }


def test_metadata_round_trip():
    origin = Origin("IV:R", "C1", "200.2", "America/Chicago")
    assert Origin.from_metadata(origin.to_metadata()) == origin
    assert json.loads(origin.to_metadata()) == {"channel": "C1", "ref": "IV:R", "ts": "200.2", "tz": "America/Chicago"}
    with pytest.raises(ValueError):
        Origin.from_metadata("{}")
    with pytest.raises(ValueError):
        Origin.from_metadata("not json")
    with pytest.raises(ValueError):
        Origin.from_metadata('{"ref": "nocolon", "channel": "C1", "ts": "1"}')


def test_cancel_view_shape():
    view = cancel_view(Origin("IV:R", "C1", "200.2", "America/Chicago"), "Acme · Round 1")
    assert view["type"] == "modal"
    assert view["callback_id"] == "coord_cancel"
    assert view["title"] == {"type": "plain_text", "text": "Canceled"}
    assert view["submit"] == {"type": "plain_text", "text": "Save"}
    assert view["close"] == {"type": "plain_text", "text": "Back"}
    note = next(b for b in view["blocks"] if b.get("block_id") == "note")
    assert note["optional"] is True
    assert note["label"] == {"type": "plain_text", "text": "Reason (optional)"}
    assert note["element"]["type"] == "plain_text_input"
    assert note["element"]["action_id"] == "note_input"
    assert note["element"]["multiline"] is True
    assert note["element"]["max_length"] == 2000
    assert view["blocks"][0]["type"] == "context"
    assert json.loads(view["private_metadata"])["ts"] == "200.2"
    assert len(view["private_metadata"]) <= 3000


def test_reject_view_shape_and_title_limits():
    origin = Origin("IV:R", "C1", "200.2")
    reject = reject_view(origin, "x" * 500)
    assert reject["callback_id"] == "coord_reject"
    assert reject["title"]["text"] == "Rejected"
    note = next(b for b in reject["blocks"] if b.get("block_id") == "note")
    assert note["label"]["text"] == "Feedback (optional)"
    assert len(reject["blocks"][0]["elements"][0]["text"]) <= 200
    for view in (cancel_view(origin, "t"), reject, reschedule_view(origin, "t", start=NOW)):
        assert len(view["title"]["text"]) <= 24
        assert view["title"]["type"] == view["submit"]["type"] == view["close"]["type"] == "plain_text"


def test_reschedule_view_defaults_to_the_current_time_in_the_owners_zone():
    origin = Origin("IV:R", "C1", "200.2", "America/New_York")
    view = reschedule_view(origin, "Acme", start=datetime(2026, 11, 16, 18, 30, tzinfo=UTC))
    assert view["callback_id"] == "coord_reschedule"
    assert view["title"]["text"] == "Rescheduled"
    ids = [b.get("block_id") for b in view["blocks"]]
    assert ids[-3:] == ["date", "time", "note"]
    date = next(b for b in view["blocks"] if b.get("block_id") == "date")
    time = next(b for b in view["blocks"] if b.get("block_id") == "time")
    note = next(b for b in view["blocks"] if b.get("block_id") == "note")
    assert date["element"]["type"] == "datepicker"
    assert date["element"]["action_id"] == "date_input"
    assert date["element"]["initial_date"] == "2026-11-16"
    assert date["label"] == {"type": "plain_text", "text": "Date"}
    assert time["element"]["type"] == "timepicker"
    assert time["element"]["action_id"] == "time_input"
    assert time["element"]["initial_time"] == "13:30"
    assert time["label"] == {"type": "plain_text", "text": "Time (America/New_York)"}
    assert note["label"]["text"] == "Note (optional)"


def test_read_reschedule_in_the_owners_zone():
    answer = read_submission(
        submitted("coord_reschedule", date="2026-11-18", time="09:15", note="  Moved by recruiter ")
    )
    assert answer.kind == "rescheduled"
    assert answer.new_start == datetime(2026, 11, 18, 15, 15, tzinfo=UTC)
    assert answer.new_start.utcoffset().total_seconds() == 0
    assert answer.note == "Moved by recruiter"
    assert answer.origin == Origin("IV:R", "C1", "200.2", "America/Chicago")


def test_reschedule_across_dst():
    summer = read_submission(submitted("coord_reschedule", date="2027-03-14", time="09:00"))
    winter = read_submission(submitted("coord_reschedule", date="2026-11-01", time="09:00"))
    assert summer.new_start == datetime(2027, 3, 14, 14, 0, tzinfo=UTC)
    assert winter.new_start == datetime(2026, 11, 1, 15, 0, tzinfo=UTC)


def test_empty_zone_reads_as_utc():
    answer = read_submission(submitted("coord_reschedule", tz="", date="2026-11-18", time="09:15"))
    assert answer.new_start == datetime(2026, 11, 18, 9, 15, tzinfo=UTC)


def test_submission_errors():
    past = submitted("coord_reschedule", date="2026-11-16", time="05:00")  # 11:00Z, before NOW
    assert submission_errors(past, NOW) == {"time": "Pick a time in the future."}
    assert submission_errors(submitted("coord_reschedule", date=None, time="09:00"), NOW) == {"date": "Pick a date."}
    assert submission_errors(submitted("coord_reschedule", date="2026-11-20", time=None), NOW) == {
        "time": "Pick a time."
    }
    assert submission_errors(submitted("coord_reschedule", date="2026-11-20", time="09:00"), NOW) == {}


def test_note_over_the_limit_is_an_error_on_every_form():
    long = "x" * 2001
    for cb in ("coord_cancel", "coord_reject"):
        assert submission_errors(submitted(cb, note=long), NOW) == {"note": "At most 2000 characters."}
    assert submission_errors(submitted("coord_cancel", note="x" * 2000), NOW) == {}
    both = submitted("coord_reschedule", date="2026-11-20", time="09:00", note=long)
    assert submission_errors(both, NOW) == {"note": "At most 2000 characters."}


def test_cancel_and_reject_read_the_note():
    blank = read_submission(submitted("coord_cancel", note="   "))
    assert (blank.kind, blank.note, blank.new_start) == ("canceled", "", None)
    assert read_submission(submitted("coord_cancel", note=None)).note == ""
    rejected = read_submission(submitted("coord_reject", note=" bad fit "))
    assert (rejected.kind, rejected.note) == ("rejected", "bad fit")


def test_read_submission_rejects_malformed_views():
    with pytest.raises(ValueError):
        read_submission({"callback_id": "coord_cancel", "private_metadata": "{}", "state": {"values": {}}})
    with pytest.raises(ValueError):
        read_submission({"callback_id": "other", "private_metadata": Origin("I:R", "C", "1").to_metadata()})
    with pytest.raises(ValueError):
        read_submission(submitted("coord_reschedule", date="garbage", time="09:00"))


def test_malformed_metadata_gives_no_field_errors():
    view = submitted("coord_cancel", note="x", metadata="{}")
    assert submission_errors(view, NOW) == {}
    assert submission_errors({}, NOW) == {}


def test_submission_errors_never_raises(monkeypatch):
    def boom(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(modals, "_value", boom)
    errors = submission_errors(submitted("coord_cancel"), NOW)
    assert list(errors) == ["note"] and errors["note"]
    for junk in (None, 5, "x", {"state": 3}, {"callback_id": "coord_reschedule", "state": {"values": []}}):
        assert isinstance(submission_errors(junk, NOW), dict)
