"""Unit tests for the plan count models (self bids split by region)."""

import pytest
from pydantic import ValidationError

from app.models.plan_common import BidsIn, Counts, CountsIn, bids_of


def test_typed_regions_set_the_self_total():
    bids = BidsIn(bidsSelf=999, bidsSelfUs=3, bidsSelfEu=2, bidsSelfLatam=1, bidsBidder=4)
    assert (bids.bidsSelf, bids.bidsBidder) == (6, 4)
    assert CountsIn(bidsSelfLatam=5, interviewsSelf=2).bidsSelf == 5


def test_pre_split_total_is_kept_without_regions():
    assert BidsIn(bidsSelf=7).bidsSelf == 7


def test_stored_counts_are_not_recomputed():
    # A week mixing pre-split days and split days: the total is more than the regions.
    assert Counts(bidsSelf=11, bidsSelfUs=4).bidsSelf == 11


def test_regions_cannot_be_negative():
    with pytest.raises(ValidationError):
        BidsIn(bidsSelfEu=-1)


def test_bids_of_copies_every_bid_field():
    c = Counts(bidsSelf=6, bidsSelfUs=1, bidsSelfEu=2, bidsSelfLatam=3, bidsBidder=4, interviewsSelf=9)
    assert Counts(**bids_of(c)) == Counts(bidsSelf=6, bidsSelfUs=1, bidsSelfEu=2, bidsSelfLatam=3, bidsBidder=4)


def test_all_zero_profile_map_still_sums_regions():
    assert BidsIn(bidsSelfByProfile={"a": 0}, bidsSelfUs=3).bidsSelf == 3
    assert BidsIn(bidsSelfByProfile={"a": 5}, bidsSelf=9, bidsSelfUs=5).bidsSelf == 9  # put_day derives both
