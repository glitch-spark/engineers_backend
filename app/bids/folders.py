"""Bidder folder names: slugs, collision-free assignment, and the legacy-URL backfill."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from app.bids.storage import parse_folder_url
from app.models.bidder import Bidder


def slug(text: str, max_len: int = 40, empty: str = "bidder") -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:max_len].strip("-")
    return s or empty


def profile_segment(profile_name: Optional[str]) -> str:
    return slug(profile_name or "", empty="no-profile")


def folder_url(bucket: str, folder: str) -> str:
    return f"s3://{bucket}/{folder}/"


async def unique_folder(name: str) -> str:
    """First free of slug, slug-2, slug-3, ... Archived bidders still hold their folder."""
    base = slug(name)
    taken = {b.folder for b in await Bidder.find({"folder": {"$ne": None}}).to_list()}
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


async def backfill_folders() -> int:
    """Give every bidder without a folder one (from its legacy screenshot URL, else its name)."""
    updated = 0
    for b in await Bidder.find({"folder": None}).sort("_id").to_list():
        try:
            _, prefix = parse_folder_url(b.screenshotFolderUrl)
            source = prefix.rstrip("/").replace("/", "-")
        except ValueError:
            source = b.name
        b.folder = await unique_folder(source)
        await b.save()
        updated += 1
    return updated
