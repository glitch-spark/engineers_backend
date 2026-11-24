"""What a caller gets for a round: the brief PDF (the app's `interviewBriefPdf.ts`, built server side) and the
interview's linked resume. Left out of the brief on purpose: owner, caller name, applicant, interviewer."""
import html
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from app import resume_files
from app.models import Account
from app.resume_service import _html_to_pdf
from app.slack_bot import _METHOD_LABELS, _stage_label

log = logging.getLogger(__name__)

NO_RESUME_NOTE = "No resume linked — ask the interview manager."
_TEMPLATE = (Path(__file__).parent / "brief.html").read_text(encoding="utf-8")


def clean_name(s: Optional[str]) -> str:
    """A file name part safe on every OS: no path separators or reserved characters, single spaces."""
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", s or "")).strip()


def brief_filename(company: str, stage_label: str, day: Optional[str]) -> str:
    """`Brief - Acme - Tech round 1 - 2026-11-06.pdf`, safe on every OS."""
    return " - ".join(p for p in ("Brief", clean_name(company), clean_name(stage_label), day) if p) + ".pdf"


def _local(dt: Optional[datetime], tz: str) -> Optional[datetime]:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo(tz))


def _clock(d: datetime, meridiem: bool = True) -> str:
    return d.strftime("%I:%M %p" if meridiem else "%I:%M").lstrip("0")


def _day(d: datetime) -> str:
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def _when(start: Optional[datetime], end: Optional[datetime]) -> str:
    if start is None:
        return ""
    date = f"{start.strftime('%a')}, {_day(start)}"
    if end is None:
        return f"{date} · {_clock(start)} {start.strftime('%Z')}"
    same = start.strftime("%p") == end.strftime("%p")
    return f"{date} · {_clock(start, not same)} – {_clock(end)} {start.strftime('%Z')}"


def _status(value) -> str:
    key = str(getattr(value, "value", value) or "")
    return key.capitalize() if key else ""


def _profile(acc) -> str:
    if acc is None:
        return ""
    where = (acc.country or "").strip() or (acc.region or "").strip()
    return " · ".join(p for p in ((acc.name or acc.email or "").strip(), where) if p)


def _pairs(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<tr><td class="k">{html.escape(k)}</td><td>{html.escape(v)}</td></tr>' for k, v in rows if v and v.strip()
    )
    return f"<table>{body}</table>" if body else ""


def _section(title: str, body: str) -> str:
    return f"<h2>{html.escape(title)}</h2>{body}" if body else ""


def _text(value: str) -> str:
    value = (value or "").strip()
    return f'<div class="text">{html.escape(value)}</div>' if value else ""


async def brief_pdf(iv, round_id: str, tz: str) -> tuple[str, bytes]:
    """`(filename, pdf bytes)` of the brief for `round_id` (the last round when it isn't found), dates in `tz`."""
    rounds = list(iv.stageHistory or [])
    idx = next((i for i, r in enumerate(rounds) if r.id == round_id), len(rounds) - 1)
    rnd = rounds[idx] if rounds else None
    acc = await Account.get(iv.accountId)
    caller = rnd.caller if rnd and rnd.caller and rnd.caller.enabled else None
    value = (caller.methodValue if caller else "") or ""
    start, end = _local(rnd.scheduledAt, tz) if rnd else None, _local(rnd.endsAt, tz) if rnd else None
    company = (iv.companyName or "").strip() or "Untitled company"
    stage = _stage_label(rnd.stage) if rnd else ""
    previous = "".join(
        "<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in (
            _day(_local(r.scheduledAt, tz)) if r.scheduledAt else "—", _stage_label(r.stage), _status(r.status) or "—"
        )) + "</tr>"
        for r in rounds[:max(idx, 0)]
    )
    jd = iv.resumeLink.jobDescription if iv.resumeLink else ""
    sections = "".join((
        _section("Job", _pairs([
            ("Position", iv.appliedPosition or ""), ("Tech stack", iv.mainTechStack or ""),
            ("Job post", iv.jobUrl or ""), ("Profile", _profile(acc)),
        ])),
        _section("Meeting", _pairs([
            ("How", _METHOD_LABELS.get(caller.method, "") if caller and caller.method else ""),
            ("Link" if re.match(r"https?://", value, re.I) else "Details", value),
        ])),
        _section("Notes", _text(rnd.note if rnd else "")),
        _section("Previous rounds", (
            f'<table class="rounds"><tr><th>Date</th><th>Round</th><th>Result</th></tr>{previous}</table>' if previous else ""
        )),
        _section("Job description", _text(jd or "")),
    ))
    page = _TEMPLATE.format(
        company=html.escape(company),
        subtitle=html.escape(" · ".join(p for p in (stage, _status(rnd.status) if rnd else "") if p)),
        when=html.escape(_when(start, end)),
        sections=sections,
    )
    name = brief_filename(company, stage, start.strftime("%Y-%m-%d") if start else None)
    return name, await _html_to_pdf(page)


async def round_files(iv, round_id: str, tz: str) -> tuple[list[tuple[str, bytes]], Optional[str]]:
    """The brief plus the linked resume, and a note when the resume is missing. A failing brief raises; a resume that is
    gone (FileUnavailable) only sets the note; any other resume error raises so the caller retries."""
    out = [await brief_pdf(iv, round_id, tz)]
    link = iv.resumeLink
    try:
        found = await resume_files.read_linked_resume(
            kind=link.kind if link else "none",
            resume_job_id=str(link.resumeJobId) if link and link.resumeJobId else None,
            account_id=str(iv.accountId), resume_id=link.resumeId if link else None,
        )
    except resume_files.FileUnavailable as exc:  # gone for good: the brief still goes out; other errors propagate
        log.warning("resume for interview %s unavailable: %s", iv.id, type(exc).__name__)
        found = None
    if found is None:
        return out, NO_RESUME_NOTE
    return out + [found], None
