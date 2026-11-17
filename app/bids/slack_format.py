"""Slack Block Kit formatting for the daily and weekly bid reports."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from app.bids.windows import Kind
from app.models.bid_report import BidReportRow
from app.slack_bot import _country_flag, _slack_escape
from app.slack_transactions import fmt_usd

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


def _is_empty(r: BidReportRow) -> bool:
    """A bidder with nothing to report: no error and no approved, pending or rejected bids."""
    return not r.error and not (r.count or 0) and not (r.pending or 0) and not (r.rejected or 0)


def _stats(rows: Sequence[BidReportRow]) -> str:
    """The bid total of the rows in the page's words: "A approved · P pending[ · R rejected]" from bid records,
    "N bids" when every counted row comes from a folder. Rows with an error count for nothing."""
    ok = [r for r in rows if not r.error]
    approved = sum(r.count or 0 for r in ok)
    records = [r for r in ok if r.pending is not None]
    if ok and not records:
        return _bids(approved)
    text = f"{approved:,} approved · {sum(r.pending or 0 for r in records):,} pending"
    rejected = sum(r.rejected or 0 for r in records)
    return text + (f" · {rejected:,} rejected" if rejected > 0 else "")


def _date(dt: datetime) -> str:
    return f"{dt:%a %b} {dt.day}"


def _time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")  # no %-I on Windows


def _section(text: str, accessory: Optional[dict] = None) -> dict:
    block = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if accessory:
        block["accessory"] = accessory
    return block


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
    pay = f" × {fmt_usd(r.rate)} = {fmt_usd(r.amount or 0)}" if kind == "weekly" else ""
    if r.pending is None:  # folder count
        return f"• {label} — {_bids(count)}{pay}"
    line = f"• {label} — {count:,} approved{pay} · {r.pending:,} pending"
    if (r.rejected or 0) > 0:
        line += f" · {r.rejected:,} rejected"
    return line


def _my_bids_line(profiles: Mapping[str, int]) -> Optional[str]:
    """"My bids — 35 (Arda 12 · Cory 23)": the owner's own bids per profile; never part of any total."""
    shown = sorted(((name, n) for name, n in profiles.items() if (n or 0) > 0), key=lambda p: p[0].lower())
    if not shown:
        return None
    return (f"My bids — {sum(n for _, n in shown):,} ("
            + " · ".join(f"{_slack_escape(name)} {n:,}" for name, n in shown) + ")")


def _owner_sections(kind: Kind, label: str, rows: Sequence[BidReportRow],
                    my_bids_line: Optional[str] = None) -> list[str]:
    """One mrkdwn text per owner; split into "(cont.)" sections to stay under Slack's 3,000-char limit."""
    ok = [r for r in rows if not r.error]
    failed = len(rows) - len(ok)
    head = f"*{_owner_label(label)}*"
    if rows:
        head += f" — {_stats(rows)}"
        if kind == "weekly":
            head += f" · *{fmt_usd(round(sum(r.amount or 0 for r in ok), 2))}*"
        if failed:
            head += f" · {failed} failed"
    ordered = sorted(rows, key=lambda r: (bool(r.error), -(r.count or 0), r.bidderName.lower()))
    lines = [_bidder_line(kind, r) for r in ordered]
    if my_bids_line:
        lines.append(my_bids_line)
    cont = f"{head} (cont.)"
    sections, current = [], head
    for line in lines:
        if len(current) + 1 + len(line) > MAX_SECTION_CHARS:
            sections.append(current)
            current = cont
        current += "\n" + line
    sections.append(current)
    return sections


def _owner_button(kind: Kind, app_url: str, owner_id: str, period_key: str, pending: int) -> dict:
    if kind == "weekly":
        text, query = "Open Pay", f"section=pay&owner={owner_id}&range=pay_week&date={period_key}"
    elif pending > 0:
        text, query = f"Review {pending:,} pending", f"owner={owner_id}&mode=focus&review=day&at={period_key}"
    else:
        text, query = "Open Bids", f"owner={owner_id}&range=pay_week&date={period_key}"
    return {"type": "button", "text": {"type": "plain_text", "text": text},
            "url": f"{app_url}/bidders?{query}", "action_id": f"bids-link-{owner_id}"}


def format_report(kind: Kind, start: datetime, end: datetime, rows: Sequence[BidReportRow],
                  owners: Mapping[str, Owner], tz: ZoneInfo, app_url: str,
                  updated_at: Optional[datetime] = None,
                  my_bids: Optional[Mapping[str, Mapping[str, int]]] = None) -> tuple[str, list[list[dict]]]:
    """Return (fallback_text, messages); messages[0] is the channel post, the rest are thread replies.
    Only bidders with bids are listed. `my_bids` maps owner id -> {profile name: own bids}; it adds a line to the
    owner's section and is never counted in a total. `updated_at` is set when a posted report is edited after
    a review, and shown in the footer."""
    start, end = start.astimezone(tz), end.astimezone(tz)
    period_key = rows[0].periodKey if rows else end.date().isoformat()  # the report day the buttons open
    listed = [r for r in rows if not _is_empty(r)]
    ok = [r for r in listed if not r.error]
    money = fmt_usd(round(sum(r.amount or 0 for r in ok), 2))
    link = f"<{app_url}/bidders|View in app>"
    buttons = app_url.startswith("http")  # Slack refuses a link button without a usable URL
    my_lines = {str(k): line for k, v in (my_bids or {}).items() if (line := _my_bids_line(v))}

    by_owner: dict[str, list[BidReportRow]] = defaultdict(list)
    for r in listed:
        by_owner[str(r.ownerId)].append(r)
    for k in my_lines:
        by_owner.setdefault(k, [])  # an owner with only My bids still gets a section
    resolved = sorted(((k, owners.get(k, UNKNOWN_OWNER), rs) for k, rs in by_owner.items()),
                      key=lambda p: p[1].name.lower())

    if kind == "daily":
        header = (f"📨 *Daily bids — {_date(end)}* · {start:%a} {_time(start)} → "
                  f"{end:%a} {_time(end)} {end:%Z}")
        fallback = f"Daily bids — {_date(end)}: "
        empty = "No bids today."
    else:
        header = (f"💰 *Weekly bids & pay — week ending {_date(end)}* · {_date(start)} {_time(start)} → "
                  f"{_date(end)} {_time(end)} {end:%Z}")
        fallback = f"Weekly bids & pay — week ending {_date(end)}: "
        empty = "No bids this week."

    if not resolved:
        return fallback + "no bids", [[_section(header), _section(empty), _section(link)]]

    stats = _stats(ok) + (f" · {money}" if kind == "weekly" else "")
    fallback += stats
    footer = f"*Total: {stats} · {len(ok):,} bidder{'' if len(ok) == 1 else 's'} with bids* · {link}"
    if updated_at is not None:
        updated_at = updated_at.astimezone(tz)
        footer += f" · updated {updated_at:%a} {_time(updated_at)}"

    blocks = [_section(header)]
    for owner_id, owner, owner_rows in resolved:
        accessory = None
        if buttons:
            pending = sum(r.pending or 0 for r in owner_rows if not r.error)
            accessory = _owner_button(kind, app_url, owner_id, period_key, pending)
        for i, text in enumerate(_owner_sections(kind, owner.label, owner_rows, my_lines.get(owner_id))):
            blocks.append(_section(text, accessory if i == 0 else None))
    blocks.append(_section(footer))
    messages = [blocks[i:i + MAX_BLOCKS] for i in range(0, len(blocks), MAX_BLOCKS)]
    return fallback, messages
