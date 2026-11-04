"""A day's self bids typed per profile (Account) and the region totals derived from them.

Region fields are fixed at save time: a profile's region changing later doesn't
rewrite past days.
"""

from typing import Iterable, Optional

from beanie import PydanticObjectId
from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.models import Account

# Profile region -> the Daily Plan region field. Canada, Asia and no region count in `bidsSelf` only.
REGION_FIELD = {"US": "bidsSelfUs", "EU": "bidsSelfEu", "Latam": "bidsSelfLatam"}


class _PlanProfile(BaseModel):
    """The Account fields a plan needs: a whole profile also carries its templates and resumes."""

    id: PydanticObjectId = Field(alias="_id")
    name: str
    region: Optional[str] = None
    archived: bool = False


def derive_self_bids(by_profile: dict[str, int], regions: dict[str, Optional[str]]) -> dict[str, int]:
    """The self total and region fields for self bids typed per profile id."""
    out = {"bidsSelf": sum(by_profile.values()), "bidsSelfUs": 0, "bidsSelfEu": 0, "bidsSelfLatam": 0}
    for pid, n in by_profile.items():
        field = REGION_FIELD.get(regions.get(pid) or "")
        if field:
            out[field] += n
    return out


async def owner_profiles(owner_id) -> list[dict]:
    """Every profile the owner created (archived included, so old days still resolve), by name."""
    accounts = await Account.find({"createdBy": owner_id}).project(_PlanProfile).to_list()
    return [
        {"id": str(a.id), "name": a.name, "region": a.region, "archived": a.archived}
        for a in sorted(accounts, key=lambda a: a.name.lower())
    ]


async def profile_regions(profiles: list[dict], ids: Iterable[str]) -> dict[str, Optional[str]]:
    """{profile id: region} for the `ids` among the owner's `profiles`. An id that is no profile at all (deleted
    since the day was saved) is left out, so the day stays saveable; 422 on another user's profile."""
    regions = {p["id"]: p["region"] for p in profiles}
    ids = list(ids)
    unknown = [PydanticObjectId(pid) for pid in ids if pid not in regions and PydanticObjectId.is_valid(pid)]
    if unknown:
        theirs = await Account.get_motor_collection().find_one({"_id": {"$in": unknown}}, {"_id": 1})
        if theirs:
            raise HTTPException(status_code=422, detail=f"Not your profile: {theirs['_id']}")
    return {pid: regions[pid] for pid in ids if pid in regions}
