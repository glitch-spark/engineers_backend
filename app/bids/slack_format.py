"""Slack Block Kit formatting for the daily and weekly bid reports."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from app.bids.windows import Kind
from app.models.bid_report import BidReportRow
from app.slack import _fmt_usd
from app.slack_bot import _country_flag, _slack_escape

MAX_SECTION_CHARS = 3000
MAX_BLOCKS = 50
_USER_MENTION = re.compile(r"<@[UW][A-Z0-9]+>")


@dataclass(frozen=True)
class Owner:
    name: str   # used for sorting
    label: str  # Slack mention or plain name


UNKNOWN_OWNER = Owner("Unknown owner", "Unknown owner")


def _bids(n: int) -> str:
    return f"{n:,} {'bid' if n == 1 else 'bids'}"


def _date(dt: datetime) -> str:
    return f"{dt:%a %b} {dt.day}"


def _time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")  # no %-I on Windows


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _owner_label(label: str) -> str:
    """A Slack user mention passes through; any other text is escaped so it cannot ping or link."""
    return label if _USER_MENTION.fullmatch(label) else _slack_escape(label)


def _bidder_line(kind: Kind, r: BidReportRow) -> str:
    name = _slack_escape(r.bidderName)  # user-entered: keep <!channel> or <url|text> from rendering
    if r.error:
        return f"• ⚠️ {name} — couldn't read screenshot folder"
    flag = _country_flag(r.country)
    label = f"{flag} {name}" if flag else name
    if r.profileName:
        label += f" · {_slack_escape(r.profileName)}"
    count = r.count or 0
    if kind == "weekly":
        return f"• {label} — {_bids(count)} × {_fmt_usd(r.rate)} = {_fmt_usd(r.amount or 0)}"
    return f"• {label} — {count:,}"


def _owner_sections(kind: Kind, label: str, rows: Sequence[BidReportRow]) -> list[str]:
    """One mrkdwn text per owner; split into "(cont.)" sections to stay under Slack's 3,000-char limit."""
    ok = [r for r in rows if not r.error]
    failed = len(rows) - len(ok)
    head = f"*{_owner_label(label)}* — {_bids(sum(r.count or 0 for r in ok))}"
    if kind == "weekly":
        head += f" · *{_fmt_usd(round(sum(r.amount or 0 for r in ok), 2))}*"
    if failed:
        head += f" · {failed} failed"
    ordered = sorted(rows, key=lambda r: (bool(r.error), -(r.count or 0), r.bidderName.lower()))
    cont = f"{head} (cont.)"
    sections, current = [], head
    for line in (_bidder_line(kind, r) for r in ordered):
        if len(current) + 1 + len(line) > MAX_SECTION_CHARS:
            sections.append(current)
            current = cont
        current += "\n" + line
    sections.append(current)
    return sections


def format_report(kind: Kind, start: datetime, end: datetime, rows: Sequence[BidReportRow],
                  owners: Mapping[str, Owner], tz: ZoneInfo, app_url: str) -> tuple[str, list[list[dict]]]:
    """Return (fallback_text, messages); messages[0] is the channel post, the rest are thread replies."""
    start, end = start.astimezone(tz), end.astimezone(tz)
    ok = [r for r in rows if not r.error]
    total = sum(r.count or 0 for r in ok)
    money = _fmt_usd(round(sum(r.amount or 0 for r in ok), 2))

    if kind == "daily":
        header = (f"📨 *Daily bids — {_date(end)}* · {start:%a} {_time(start)} → "
                  f"{end:%a} {_time(end)} {end:%Z}")
        footer_total = f"*Total: {_bids(total)} · {len(rows):,} bidder{'' if len(rows) == 1 else 's'}*"
        fallback = f"Daily bids — {_date(end)}: {_bids(total)}"
    else:
        header = (f"💰 *Weekly bids & pay — week ending {_date(end)}* · {_date(start)} {_time(start)} → "
                  f"{_date(end)} {_time(end)} {end:%Z}")
        footer_total = f"*Total: {_bids(total)} · {money}*"
        fallback = f"Weekly bids & pay — week ending {_date(end)}: {_bids(total)} · {money}"
    footer = f"{footer_total} · <{app_url}/bidders|View in app>"

    by_owner: dict[str, list[BidReportRow]] = defaultdict(list)
    for r in rows:
        by_owner[str(r.ownerId)].append(r)
    resolved = sorted(((owners.get(k, UNKNOWN_OWNER), rs) for k, rs in by_owner.items()),
                      key=lambda p: p[0].name.lower())

    blocks = [_section(header)]
    for owner, owner_rows in resolved:
        blocks.extend(_section(t) for t in _owner_sections(kind, owner.label, owner_rows))
    blocks.append(_section(footer))
    messages = [blocks[i:i + MAX_BLOCKS] for i in range(0, len(blocks), MAX_BLOCKS)]
    return fallback, messages
