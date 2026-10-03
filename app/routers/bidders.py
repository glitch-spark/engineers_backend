"""Bidder routes (owner-only) and the cron fallback."""

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from app.bids.live_counts import CACHE, live_counts
from app.bids.reports import run_bid_reports_tick
from app.bids.schemas import BidderIn, bidder_out, report_out
from app.bids.storage import get_bid_storage
from app.bids.windows import schedule_from_settings
from app.config import get_settings
from app.deps import get_current_user
from app.models import Account, UserExtra
from app.models.bid_report import BidReport
from app.models.bidder import Bidder

router = APIRouter(prefix="/bidders", tags=["bidders"])
cron_router = APIRouter(prefix="/internal/bids", tags=["bids-cron"])


def ensure_owner(bidder, user):
    """404 (not 403) for a missing or foreign bidder; archived ones stay readable. No admin exemption."""
    if bidder is None or bidder.createdBy != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bidder not found")
    return bidder


def can_select_profile(account, user) -> bool:
    return (
        account is not None
        and not account.archived
        and (user.role == "admin" or account.createdBy == user.id)
    )


async def _load_owned(bidder_id: str, user) -> Bidder:
    if not PydanticObjectId.is_valid(bidder_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bidder not found")
    return ensure_owner(await Bidder.get(PydanticObjectId(bidder_id)), user)


async def _check_profile(
    body: BidderIn, user, current: Optional[PydanticObjectId] = None
) -> Optional[Account]:
    """The chosen profile's account; a profile the bidder already has is kept even if since archived."""
    if not body.profileId:
        return None
    account = await Account.get(PydanticObjectId(body.profileId))
    if body.profileId != str(current) and not can_select_profile(account, user):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": ["body", "profileId"], "msg": "Choose one of your profiles", "type": "value_error"}],
        )
    return account


@router.get("")
async def list_bidders(
    user: Annotated[UserExtra, Depends(get_current_user)],
    archived: bool = False,
) -> dict:
    bidders = await Bidder.find(
        Bidder.createdBy == user.id,
        {"archivedAt": {"$ne": None}} if archived else {"archivedAt": None},
    ).sort("name").to_list()
    ids = list({b.profileId for b in bidders if b.profileId})
    accounts = await Account.find({"_id": {"$in": ids}}).to_list() if ids else []
    names = {a.id: a.name for a in accounts}
    return {"bidders": [bidder_out(b, names.get(b.profileId)) for b in bidders]}


@router.post("")
async def create_bidder(
    body: BidderIn, user: Annotated[UserExtra, Depends(get_current_user)]
) -> dict:
    account = await _check_profile(body, user)
    bidder = Bidder(
        createdBy=user.id,
        name=body.name,
        country=body.country,
        profileId=PydanticObjectId(body.profileId) if body.profileId else None,
        rate=body.rate,
        screenshotFolderUrl=body.screenshotFolderUrl,
    )
    await bidder.insert()
    return bidder_out(bidder, account.name if account else None)


# Declared before "/{bidder_id}" routes so "live-counts" is not read as an id.
@router.get("/live-counts")
async def get_live_counts(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    bidders = await Bidder.find(Bidder.createdBy == user.id, {"archivedAt": None}).to_list()
    return await live_counts(
        bidders, datetime.now(timezone.utc), schedule_from_settings(), get_bid_storage(), CACHE
    )


@router.put("/{bidder_id}")
async def update_bidder(
    bidder_id: str, body: BidderIn, user: Annotated[UserExtra, Depends(get_current_user)]
) -> dict:
    bidder = await _load_owned(bidder_id, user)
    account = await _check_profile(body, user, current=bidder.profileId)
    bidder.name = body.name
    bidder.country = body.country
    bidder.profileId = PydanticObjectId(body.profileId) if body.profileId else None
    bidder.rate = body.rate
    bidder.screenshotFolderUrl = body.screenshotFolderUrl
    await bidder.save()
    return bidder_out(bidder, account.name if account else None)


@router.delete("/{bidder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_bidder(
    bidder_id: str, user: Annotated[UserExtra, Depends(get_current_user)]
) -> Response:
    bidder = await _load_owned(bidder_id, user)
    if bidder.archivedAt is None:
        bidder.archivedAt = datetime.now(timezone.utc)
        await bidder.save()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{bidder_id}/reports")
async def bidder_reports(
    bidder_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    kind: Literal["daily", "weekly"] = "daily",
    limit: int = Query(30, ge=1, le=200),
) -> dict:
    bidder = await _load_owned(bidder_id, user)
    reports = (
        await BidReport.find(BidReport.bidderId == bidder.id, BidReport.kind == kind)
        .sort("-periodKey")
        .limit(limit)
        .to_list()
    )
    return {"reports": [report_out(r) for r in reports]}


@cron_router.post("/tick")
async def bids_tick(
    x_cron_secret: Annotated[Optional[str], Header()] = None,
) -> dict:
    s = get_settings()
    expected = (s.SLACK_BOT_CRON_SECRET or "").strip()
    if not expected or (x_cron_secret or "").strip() != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    result = await run_bid_reports_tick()
    return {"ok": True, **result}
