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
    base = dict(name=" Ana Silva ", country="br", profileId=str(PydanticObjectId()), rate=0.5,
                screenshotFolderUrl=" s3://shots/bids/ana/ ")
    base.update(over)
    return base


def test_bidder_in_normalizes():
    b = BidderIn(**valid())
    assert (b.name, b.country, b.rate, b.screenshotFolderUrl) == ("Ana Silva", "BR", 0.5, "s3://shots/bids/ana/")


def test_blank_optional_fields_become_none():
    b = BidderIn(**valid(country="", profileId=""))
    assert b.country is None and b.profileId is None


@pytest.mark.parametrize("field,value", [
    ("name", "   "), ("name", "x" * 101), ("country", "BRA"), ("country", "1A"),
    ("rate", -0.01), ("rate", 1000.01), ("rate", 0.505), ("profileId", "nope"),
    ("screenshotFolderUrl", "https://example.com/a/"), ("screenshotFolderUrl", "s3://shots"),
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


@pytest.mark.parametrize("value", [NAIVE, AWARE])
def test_bidder_out_emits_utc_datetimes(value):
    pid = PydanticObjectId()
    b = SimpleNamespace(id=PydanticObjectId(), name="Ana", country="BR", profileId=pid, rate=0.5,
                        screenshotFolderUrl="s3://b/ana/", archivedAt=value, createdAt=value)
    out = bidder_out(b, "Backend")
    assert out["archivedAt"] == out["createdAt"] == "2026-10-01T23:30:00Z"
    assert out["profileId"] == str(pid) and out["profileName"] == "Backend"


def test_bidder_out_optional_fields():
    b = SimpleNamespace(id=PydanticObjectId(), name="Ana", country=None, profileId=None, rate=0.5,
                        screenshotFolderUrl="s3://b/ana/", archivedAt=None, createdAt=NAIVE)
    out = bidder_out(b, None)
    assert out["archivedAt"] is None and out["profileId"] is None and out["profileName"] is None


@pytest.mark.parametrize("value", [NAIVE, AWARE])
def test_report_out_emits_utc_datetimes(value):
    r = SimpleNamespace(kind="daily", periodKey="2026-10-02", periodStart=value, periodEnd=value + timedelta(days=1),
                        count=4, rate=0.5, amount=2.0, error=None)
    out = report_out(r)
    assert out["periodStart"] == "2026-10-01T23:30:00Z" and out["periodEnd"] == "2026-10-02T23:30:00Z"
    assert (out["count"], out["amount"]) == (4, 2.0)


def test_report_out_keeps_missing_count_and_amount():
    r = SimpleNamespace(kind="weekly", periodKey="2026-10-03", periodStart=NAIVE, periodEnd=NAIVE,
                        count=None, rate=0.5, amount=None, error="AccessDenied")
    out = report_out(r)
    assert out["count"] is None and out["amount"] is None and out["error"] == "AccessDenied"
