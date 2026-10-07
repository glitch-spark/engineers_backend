"""Where a row's job description lives. A fetched description is already kept per URL in JobPostingCache, so rows
don't store their own copy of it (that was half of job_apply_rows): `jdText` is written as None for fetched sources
and filled back in on load with `hydrate`. Text that exists nowhere else for sure stays on the row: read from the
sheet, or a person's correction (its JobInfo can be reset while other rows still use the text). A cached posting is
reused for 24 hours and kept 30 days, longer than a row lives, so the lookup finds it; if it's gone the row simply
has no description, like an unread one."""
from __future__ import annotations

from typing import Iterable, Optional

ROW_SOURCES = {None, "sheet", "manual"}  # stored on the row itself


def persisted(jd_text: Optional[str], jd_source: Optional[str]) -> Optional[str]:
    """The value to store in the row's jdText."""
    return jd_text if jd_source in ROW_SOURCES else None


async def hydrate(rows: Iterable) -> None:
    """Fill in jdText, in place, on rows whose description lives in the posting cache."""
    from app.models.job_apply import JobPostingCache

    need = [r for r in rows if r is not None and not r.jdText and r.jdSource not in ROW_SOURCES and r.url]
    if not need:
        return
    urls = sorted({r.url for r in need})
    texts: dict[str, str] = {}
    async for d in JobPostingCache.get_motor_collection().find({"url": {"$in": urls}}, {"url": 1, "text": 1}):
        texts[d["url"]] = d.get("text") or ""
    for r in need:
        r.jdText = texts.get(r.url) or None
