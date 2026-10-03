import os
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import PydanticObjectId
from pydantic import ValidationError

from app.bids.schemas import BidderIn
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
    assert [list(ix["key"].items()) for ix in unique] == [[("kind", 1), ("periodKey", 1), ("bidderId", 1)]]
