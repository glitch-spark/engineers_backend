"""One-off backfill: convert legacy flat contact fields → Account.contactItems.

Run once after deploying the simplified Profile schema. Idempotent — accounts
that already have contactItems populated are skipped. After this completes,
the legacy fields (email/phone/address/github/linkedin/website/twitter/
customLinks/contactLabels/label/targetPages/promptOverrides) are unset on each
document so the collection stays clean.

Run:
    cd engineers_backend
    .venv/bin/python -m scripts.backfill_contact_items
"""

from __future__ import annotations

import asyncio
import os
import sys

from motor.motor_asyncio import AsyncIOMotorClient


LEGACY_FLAT_FIELDS = [
    "email", "phone", "address", "github", "linkedin", "website", "twitter",
    "customLinks", "contactLabels", "label", "targetPages", "promptOverrides",
    "styleMode", "styleMarkdown", "styleConfig", "styles", "screeningPromptMode",
]


def _build_items_from_legacy(doc: dict) -> list[dict]:
    """Synthesize contactItems[] from the legacy flat fields. Each present
    value becomes a {display, url, show=True} entry. Empty values become
    {display: "", url: "", show: False} so the user can see them in the editor
    and fill them in. Order: email, phone, address, github, linkedin, website,
    twitter, then customLinks. Per-type display overrides from contactLabels
    apply to display text.
    """
    labels = doc.get("contactLabels") or {}
    rows: list[dict] = []

    LABELS = {
        "email": "Email", "phone": "Phone", "address": "Address",
        "github": "GitHub", "linkedin": "LinkedIn",
        "website": "Website", "twitter": "Twitter / X",
    }

    def add(type_key: str, raw: str | None) -> None:
        raw_clean = (raw or "").strip()
        display = (labels.get(type_key) or raw_clean).strip()
        url = ""
        if raw_clean:
            if type_key == "email":
                url = f"mailto:{raw_clean}"
            elif type_key == "phone":
                url = f"tel:{raw_clean.replace(' ', '')}"
            elif type_key == "address":
                url = ""
            else:
                url = raw_clean
        rows.append({
            "label": LABELS.get(type_key, type_key.title()),
            "display": display,
            "url": url,
            "show": bool(raw_clean),
        })

    add("email", doc.get("email"))
    add("phone", doc.get("phone"))
    add("address", doc.get("address"))
    add("github", doc.get("github"))
    add("linkedin", doc.get("linkedin"))
    add("website", doc.get("website"))
    add("twitter", doc.get("twitter"))

    for link in doc.get("customLinks") or []:
        if not isinstance(link, dict):
            continue
        display = (link.get("label") or "").strip()
        url = (link.get("url") or "").strip()
        if not display and not url:
            continue
        rows.append({"label": "Custom", "display": display or url, "url": url, "show": True})

    return rows


async def main() -> None:
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        print("MONGODB_URI not set", file=sys.stderr)
        sys.exit(1)

    client = AsyncIOMotorClient(uri)
    db = client.get_default_database()
    coll = db["accounts"]

    migrated = 0
    skipped = 0
    cleaned_only = 0

    async for doc in coll.find({}):
        _id = doc["_id"]
        existing = doc.get("contactItems")
        needs_items = not (isinstance(existing, list) and len(existing) > 0)

        update_set: dict = {}
        if needs_items:
            update_set["contactItems"] = _build_items_from_legacy(doc)
            # Promote legacy `label` → `title` only when title isn't already set.
            if not (doc.get("title") or "").strip() and (doc.get("label") or "").strip():
                update_set["title"] = doc["label"]
            migrated += 1
        else:
            skipped += 1

        # Always unset the legacy flat fields (idempotent).
        update_unset = {f: "" for f in LEGACY_FLAT_FIELDS if f in doc}
        if not needs_items and update_unset:
            cleaned_only += 1

        ops: dict = {}
        if update_set:
            ops["$set"] = update_set
        if update_unset:
            ops["$unset"] = update_unset
        if ops:
            await coll.update_one({"_id": _id}, ops)

    print(f"migrated={migrated} skipped_with_items={skipped} cleaned_only={cleaned_only}")


if __name__ == "__main__":
    asyncio.run(main())
