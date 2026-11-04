"""A day's self bids typed per profile (Account) and the region totals derived from them.

Region fields are fixed at save time: a profile's region changing later doesn't
rewrite past days.
"""

from typing import Iterable, Optional

from fastapi import HTTPException

from app.models import Account

# Profile region -> the Daily Plan region field. Canada, Asia and no region count in `bidsSelf` only.
REGION_FIELD = {"US": "bidsSelfUs", "EU": "bidsSelfEu", "Latam": "bidsSelfLatam"}


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
    accounts = await Account.find({"createdBy": owner_id}).to_list()
    return [
        {"id": str(a.id), "name": a.name, "region": a.region, "archived": a.archived}
        for a in sorted(accounts, key=lambda a: a.name.lower())
    ]


async def profile_regions(owner_id, ids: Iterable[str]) -> dict[str, Optional[str]]:
    """{profile id: region} for `ids`; 422 on an id that isn't one of the owner's profiles."""
    regions = {p["id"]: p["region"] for p in await owner_profiles(owner_id)}
    ids = list(ids)
    for pid in ids:
        if pid not in regions:
            raise HTTPException(status_code=422, detail=f"Not your profile: {pid}")
    return {pid: regions[pid] for pid in ids}
