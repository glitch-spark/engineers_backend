"""Unit tests for list range matching and calendar round flattening (no database)."""

import os
from datetime import datetime

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.routers.interviews import flatten_rounds, range_match  # noqa: E402

FROM = datetime(2026, 9, 28)
TO = datetime(2026, 10, 5)


def test_range_match_shape():
    rng = {"$gte": FROM, "$lte": TO}
    assert range_match(FROM, TO) == {"$or": [
        {"stageHistory": {"$elemMatch": {"scheduledAt": rng}}},
        {"stageHistory": {"$size": 0}, "scheduledAt": rng},
    ]}
    assert range_match(None, None) == {}
    assert range_match(FROM, None)["$or"][0] == {"stageHistory": {"$elemMatch": {"scheduledAt": {"$gte": FROM}}}}


def _iv(*rounds, company="Acme"):
    return {
        "_id": "iv1",
        "companyName": company,
        "accountId": {"_id": "a1", "name": "Jo Kim", "email": "jo@x.com"},
        "createdBy": {"_id": "u1"},
        "ownerName": "Owner One",
        "stageHistory": list(rounds),
    }


def _round(rid, stage, when, status="scheduled", caller=False):
    return {
        "id": rid, "stage": stage, "status": status, "scheduledAt": when,
        "endsAt": None, "caller": {"enabled": True} if caller else None,
    }


def test_flatten_rounds_filters_by_round_range_stage_status():
    iv = _iv(
        _round("r1", "intro", datetime(2026, 9, 22, 14), status="passed"),
        _round("r2", "tech_round_1", datetime(2026, 10, 1, 15)),
    )
    rows = flatten_rounds([iv], from_dt=FROM, to_dt=TO)
    assert [r["roundId"] for r in rows] == ["r2"]
    row = rows[0]
    assert row["interviewId"] == "iv1"
    assert row["companyName"] == "Acme"
    assert row["profileLabel"] == "Jo Kim"
    assert row["ownerId"] == "u1" and row["ownerName"] == "Owner One"
    assert row["scheduledAt"].startswith("2026-10-01T15:00")

    canceled = _iv(_round("r3", "panel", datetime(2026, 9, 30, 9), status="canceled"))
    assert [r["roundId"] for r in flatten_rounds([iv, canceled], from_dt=FROM, to_dt=TO, status="canceled")] == ["r3"]
    assert [r["roundId"] for r in flatten_rounds([iv, canceled], from_dt=FROM, to_dt=TO, stage="panel")] == ["r3"]


def test_flatten_rounds_end_is_exclusive():
    iv = _iv(_round("r1", "intro", TO))
    assert flatten_rounds([iv], from_dt=FROM, to_dt=TO) == []


def test_flatten_rounds_sorted_and_has_caller():
    a = _iv(_round("late", "intro", datetime(2026, 10, 2, 16), caller=True))
    b = _iv(_round("early", "intro", "2026-09-29T09:00:00"))  # ISO string input
    rows = flatten_rounds([a, b], from_dt=FROM, to_dt=TO)
    assert [r["roundId"] for r in rows] == ["early", "late"]
    assert [r["hasCaller"] for r in rows] == [False, True]
