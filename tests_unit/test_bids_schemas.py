import os
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from beanie import PydanticObjectId
from pydantic import ValidationError

from app.bids.schemas import BidderIn, bidder_out, report_out
from app.models.bid_report import BidReport
from app.models.bidder import Bidder


def valid(**over):
    base = dict(name=" Ana Silva ", country="br", profileId=str(PydanticObjectId()), rate=0.5)
    base.update(over)
    return base


def test_bidder_in_normalizes():
    b = BidderIn(**valid())
    assert (b.name, b.country, b.rate) == ("Ana Silva", "BR", 0.5)


def test_blank_optional_fields_become_none():
    b = BidderIn(**valid(country="", profileId=""))
    assert b.country is None and b.profileId is None


@pytest.mark.parametrize("field,value", [
    ("name", "   "), ("name", "x" * 101), ("country", "BRA"), ("country", "1A"),
    ("rate", -0.01), ("rate", 1000.01), ("rate", 0.505), ("profileId", "nope"),
])
def test_bidder_in_rejects(field, value):
    with pytest.raises(ValidationError) as exc:
        BidderIn(**valid(**{field: value}))
    assert exc.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("rate", [0, 0.1, 0.29, 19.99, 1000])
def test_two_decimal_rates_accepted(rate):
    assert BidderIn(**valid(rate=rate)).rate == rate


def test_collections_and_unique_report_index():
    assert Bidder.Settings.name == "bidders" and BidReport.Settings.name == "bidreports"
    assert Bidder.model_fields["archivedAt"].default is None
    unique = [ix.document for ix in BidReport.Settings.indexes if ix.document.get("unique")]
    assert [list(ix["key"].items()) for ix in unique] == [[("bidderId", 1), ("kind", 1), ("periodKey", 1)]]
    # No other index may share the unique one's field set, or Beanie silently drops one of them.
    field_sets = [frozenset(ix.document["key"]) for ix in BidReport.Settings.indexes]
    assert len(field_sets) == len(set(field_sets))


NAIVE = datetime(2026, 10, 1, 23, 30)  # how Motor returns stored UTC values (not tz_aware)
AWARE = datetime(2026, 10, 1, 18, 30, tzinfo=timezone(timedelta(hours=-5)))  # same instant, CDT


LOGIN = dict(folder="ana", username=None, registeredAt=None, inviteCodeHash=None, inviteExpiresAt=None)


@pytest.mark.parametrize("value", [NAIVE, AWARE])
def test_bidder_out_emits_utc_datetimes(value):
    pid = PydanticObjectId()
    b = SimpleNamespace(id=PydanticObjectId(), name="Ana", country="BR", profileId=pid, rate=0.5,
                        screenshotFolderUrl="s3://b/ana/", archivedAt=value, createdAt=value, **LOGIN)
    out = bidder_out(b, "Backend")
    assert out["archivedAt"] == out["createdAt"] == "2026-10-01T23:30:00Z"
    assert out["profileId"] == str(pid) and out["profileName"] == "Backend"


def test_bidder_out_optional_fields():
    b = SimpleNamespace(id=PydanticObjectId(), name="Ana", country=None, profileId=None, rate=0.5,
                        screenshotFolderUrl="s3://b/ana/", archivedAt=None, createdAt=NAIVE, **LOGIN)
    out = bidder_out(b, None)
    assert out["archivedAt"] is None and out["profileId"] is None and out["profileName"] is None


@pytest.mark.parametrize("value", [NAIVE, AWARE])
def test_report_out_emits_utc_datetimes(value):
    r = SimpleNamespace(kind="daily", periodKey="2026-10-02", periodStart=value, periodEnd=value + timedelta(days=1),
                        count=4, rate=0.5, amount=2.0, error=None, pending=1, rejected=2)
    out = report_out(r)
    assert out["periodStart"] == "2026-10-01T23:30:00Z" and out["periodEnd"] == "2026-10-02T23:30:00Z"
    assert (out["count"], out["amount"]) == (4, 2.0)
    assert (out["pending"], out["rejected"]) == (1, 2)


def test_report_out_keeps_missing_count_and_amount():
    r = SimpleNamespace(kind="weekly", periodKey="2026-10-03", periodStart=NAIVE, periodEnd=NAIVE,
                        count=None, rate=0.5, amount=None, error="AccessDenied", pending=None, rejected=None)
    out = report_out(r)
    assert out["count"] is None and out["amount"] is None and out["error"] == "AccessDenied"
    assert out["pending"] is None and out["rejected"] is None


def test_bidder_status_and_login_fields():
    from app.bids.schemas import bidder_status
    base = dict(id=PydanticObjectId(), name="Ana", country=None, profileId=None, rate=0.5,
                screenshotFolderUrl="s3://b/ana/", createdAt=NAIVE, folder="ana", username="ana.s")
    invited = SimpleNamespace(**base, archivedAt=None, registeredAt=None, inviteCodeHash="h", inviteExpiresAt=AWARE)
    active = SimpleNamespace(**base, archivedAt=None, registeredAt=NAIVE, inviteCodeHash=None, inviteExpiresAt=AWARE)
    archived = SimpleNamespace(**base, archivedAt=NAIVE, registeredAt=NAIVE, inviteCodeHash=None, inviteExpiresAt=None)
    assert [bidder_status(b) for b in (invited, active, archived)] == ["invited", "active", "archived"]
    out = bidder_out(invited, None)
    assert (out["folder"], out["username"], out["inviteExpiresAt"]) == ("ana", "ana.s", "2026-10-01T23:30:00Z")
    assert bidder_out(active, None)["inviteExpiresAt"] is None and bidder_out(active, None)["registeredAt"] == "2026-10-01T23:30:00Z"
