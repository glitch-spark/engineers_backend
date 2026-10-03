"""Bidder folder names: slugs, collision-free assignment, the legacy-URL backfill and the pre-rollout report.

`python -m app.bids.folders --report` lists, read-only, the bidders whose counting moves when they register.
"""

from __future__ import annotations

import asyncio
import re
import sys
import unicodedata
from typing import Optional

from app.bids.storage import parse_folder_url
from app.config import get_settings
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


def _locate(url: Optional[str]) -> Optional[tuple[str, str]]:
    """(bucket, prefix/) of a folder URL in any accepted form, None when unparseable."""
    try:
        return parse_folder_url(url or "")
    except ValueError:
        return None


async def _taken_folders() -> set[str]:
    """Every assigned folder (archived bidders still hold theirs), plus the top-level prefix of every bidder URL
    in BID_STORAGE_BUCKET: a new folder must not land on a legacy prefix (no `bids` while `bids/ana/` counts)."""
    bucket = get_settings().BID_STORAGE_BUCKET
    taken = set()
    for b in await Bidder.find_all().to_list():
        if b.folder:
            taken.add(b.folder)
        location = _locate(b.screenshotFolderUrl)
        if bucket and location and location[0] == bucket:
            taken.add(location[1].split("/", 1)[0])
    return taken


async def unique_folder(name: str) -> str:
    """First free of slug, slug-2, slug-3, ..."""
    base = slug(name)
    taken = await _taken_folders()
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _legacy_source(b: Bidder) -> str:
    location = _locate(b.screenshotFolderUrl)
    return location[1].rstrip("/").replace("/", "-") if location else b.name


async def backfill_folders() -> int:
    """Give every bidder without a folder one, from its legacy screenshot URL (else its name).

    First pass: a bidder already counted from <BID_STORAGE_BUCKET>/<its slug>/ keeps exactly that folder, so an
    older bidder elsewhere can't take it. Then the rest, oldest first, with -2, -3, ... on a clash."""
    bucket = get_settings().BID_STORAGE_BUCKET
    pending = await Bidder.find({"folder": None}).sort("_id").to_list()
    assigned = {b.folder for b in await Bidder.find({"folder": {"$ne": None}}).to_list()}
    rest = []
    for b in pending:
        own = slug(_legacy_source(b))
        if bucket and own not in assigned and _locate(b.screenshotFolderUrl) == _locate(folder_url(bucket, own)):
            b.folder = own
            await b.save()
            assigned.add(own)
        else:
            rest.append(b)
    for b in rest:
        b.folder = await unique_folder(_legacy_source(b))
        await b.save()
    return len(pending)


async def report_rows(bucket: str) -> list[tuple[str, str, str]]:
    """(name, folder, current URL) of every non-archived bidder not yet counted from <bucket>/<folder>/.
    Registering switches its counting to that folder for the whole week in progress."""
    rows = []
    for b in await Bidder.find({"archivedAt": None}).sort("_id").to_list():
        if not b.folder or _locate(b.screenshotFolderUrl) != _locate(folder_url(bucket, b.folder)):
            rows.append((b.name, b.folder or "-", b.screenshotFolderUrl))
    return rows


def format_report(rows: list[tuple[str, str, str]]) -> str:
    if not rows:
        return "No bidder switches folders when they register."
    noun = "bidder" if len(rows) == 1 else "bidders"
    header = f"{len(rows)} {noun} will switch counting to a new folder when they register:"
    return "\n".join([header, "name\tfolder\tcurrent URL"] + ["\t".join(row) for row in rows])


async def print_report(database=None) -> int:
    """Print the report. Read-only: no index builds or migrations, unlike app startup."""
    settings = get_settings()
    if not settings.BID_STORAGE_BUCKET:
        print("BID_STORAGE_BUCKET is not set", file=sys.stderr)
        return 2
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient

    if database is None:
        database = AsyncIOMotorClient(settings.MONGODB_URI).get_default_database()
    await init_beanie(database=database, document_models=[Bidder], skip_indexes=True)
    print(format_report(await report_rows(settings.BID_STORAGE_BUCKET)))
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.bids.folders")
    parser.add_argument(
        "--report", action="store_true",
        help="list (read-only) the bidders whose counting moves to a new folder when they register",
    )
    if not parser.parse_args().report:
        parser.error("nothing to do: pass --report")
    sys.exit(asyncio.run(print_report()))
