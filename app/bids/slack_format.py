"""Slack Block Kit formatting for the daily and weekly bid reports."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from app.bids.windows import Kind
from app.models.bid_report import BidReportRow
from app.slack_bot import _slack_escape
from app.slack_transactions import fmt_usd

MAX_SECTION_CHARS = 3000
MAX_BLOCKS = 50
_USER_MENTION = re.compile(r"<@[UW][A-Z0-9]+>")

NAME_WIDTH = 18  # the name column of the owner tables; longer names are cut with "…"
DASH = "—"  # a folder row has no approved, pending or rejected


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
    "N bids" when every counted row comes from a folder. Rows with an error count for nothing. (Fallback text.)"""
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


# --- the owner table (a monospace code block) ---

def _cut(name: str, width: int = NAME_WIDTH) -> str:
    """A name for the table: one line, no backticks (they would close the code block), at most `width` chars."""
    name = " ".join(name.replace("`", "'").split())
    return name if len(name) <= width else name[:width - 1] + "…"


@dataclass(frozen=True)
class _Table:
    columns: tuple[tuple[str, int], ...]  # (title, width) of the right-aligned columns after the name

    def line(self, name: str, *cells: str) -> str:
        """One table line. Padded on the raw text and escaped afterwards, so the padding matches what Slack
        shows (Slack needs &, < and > escaped even inside a code block). A cell always keeps one space before it,
        so a number wider than its column pushes the line right rather than running into its neighbour."""
        text = name.ljust(NAME_WIDTH) + "".join(" " + c.rjust(w - 1) for c, (_, w) in zip(cells, self.columns))
        return _slack_escape(text.rstrip())

    def head(self) -> str:
        return self.line("Bidder", *(title for title, _ in self.columns))

    def rule(self) -> str:
        return "─" * (NAME_WIDTH + sum(w for _, w in self.columns))


_DAILY = _Table((("Bids", 6), ("Appr", 6), ("Pend", 6), ("Rej", 6)))  # 42 chars
_WEEKLY = _Table((("Bids", 6), ("Appr", 6), ("Rate", 7), ("Pay", 11), ("Pend", 6)))


def _table(kind: Kind) -> _Table:
    return _WEEKLY if kind == "weekly" else _DAILY


@dataclass(frozen=True)
class _Totals:
    bids: int
    approved: int
    pending: int
    rejected: int
    pay: float
    records: bool  # some row comes from bid records; folder rows have no approved, pending or rejected


def _row_bids(r: BidReportRow) -> int:
    """approved + pending + rejected for a bid-record row; the folder count for a folder row."""
    return (r.count or 0) + (r.pending or 0) + (r.rejected or 0)


def _totals(rows: Sequence[BidReportRow]) -> _Totals:
    """Totals of the rows without an error."""
    ok = [r for r in rows if not r.error]
    records = [r for r in ok if r.pending is not None]
    return _Totals(bids=sum(_row_bids(r) for r in ok), approved=sum(r.count or 0 for r in records),
                   pending=sum(r.pending or 0 for r in records), rejected=sum(r.rejected or 0 for r in records),
                   pay=round(sum(r.amount or 0 for r in ok), 2), records=bool(records))


def _totals_text(t: _Totals, bidders: Optional[int] = None) -> str:
    """"B bids[ by N bidders] · A approved · P pending[ · R rejected]"; only the bids when every row comes from a
    folder."""
    text = _bids(t.bids)
    if bidders is not None:
        text += f" by {bidders:,} bidder{'' if bidders == 1 else 's'}"
    if t.records:
        text += f" · {t.approved:,} approved · {t.pending:,} pending"
        if t.rejected > 0:
            text += f" · {t.rejected:,} rejected"
    return text


def _cells(kind: Kind, bids: int, approved: Optional[int], pending: Optional[int], rejected: Optional[int],
           rate: Optional[float], pay: float) -> tuple[str, ...]:
    def n(v: Optional[int]) -> str:
        return DASH if v is None else f"{v:,}"
    if kind == "weekly":
        return f"{bids:,}", n(approved), "" if rate is None else fmt_usd(rate), fmt_usd(pay), n(pending)
    return f"{bids:,}", n(approved), n(pending), n(rejected)


def _bidder_line(kind: Kind, r: BidReportRow) -> str:
    name = _cut(r.bidderName)  # user-entered: escaped by line(), so <!channel> or <url|text> cannot render
    if r.error:
        return _slack_escape(f"{name.ljust(NAME_WIDTH)}  couldn't read folder")
    if r.pending is None:  # folder count
        cells = _cells(kind, _row_bids(r), None, None, None, r.rate, r.amount or 0)
    else:
        cells = _cells(kind, _row_bids(r), r.count or 0, r.pending, r.rejected or 0, r.rate, r.amount or 0)
    return _table(kind).line(name, *cells)


def _totals_line(kind: Kind, t: _Totals) -> str:
    def rec(v: int) -> Optional[int]:
        return v if t.records else None
    return _table(kind).line("Bidders total", *_cells(kind, t.bids, rec(t.approved), rec(t.pending),
                                                       rec(t.rejected), None, t.pay))


def _short_profile(name: str) -> str:
    """A profile name as the My bids line shows it: the text after the last "|", cut at the first "(", trimmed.
    "Brazil | Fabricio Suzuki(Senior Data Engineer)" -> "Fabricio Suzuki". Falls back to the full name when
    that leaves nothing."""
    short = name.rsplit("|", 1)[-1].split("(", 1)[0].strip()
    return short or name


def _my_profiles(profiles: Mapping[str, int]) -> list[tuple[str, int]]:
    """The owner's own bids per profile, profiles with none left out, sorted by the shown name case-insensitively
    (the full name breaks ties)."""
    return sorted(((name, n) for name, n in profiles.items() if (n or 0) > 0),
                  key=lambda p: (_short_profile(p[0]).lower(), p[0].lower()))


def _my_bids_lines(kind: Kind, shown: Sequence[tuple[str, int]]) -> list[str]:
    """"My bids  N", then one indented line per profile. Never part of any total."""
    table = _table(kind)
    return ([table.line("My bids", f"{sum(n for _, n in shown):,}")]
            + [table.line("  " + _cut(_short_profile(name), NAME_WIDTH - 2), f"{n:,}") for name, n in shown])


def _code(lines: Sequence[str]) -> str:
    return "```\n" + "\n".join(lines) + "\n```"


def _owner_sections(kind: Kind, label: str, rows: Sequence[BidReportRow],
                    my_profiles: Sequence[tuple[str, int]] = ()) -> tuple[str, list[str]]:
    """(header, bodies) for one owner. The header is the short line that carries the button; the bodies are code
    blocks with the aligned bidder table, its totals and the My bids lines, with no accessory so they get the full
    message width. A long table splits into "(cont.)" bodies that repeat the column header, to stay under Slack's
    3,000-char section limit; bodies is empty when there are no lines."""
    ok = [r for r in rows if not r.error]
    failed = len(rows) - len(ok)
    who = f"*{_owner_label(label)}*"
    parts = []
    if ok:
        totals = _totals(ok)
        parts.append(_totals_text(totals))
        if kind == "weekly":
            parts.append(f"*{fmt_usd(totals.pay)}*")
    if failed:
        parts.append(f"{failed} failed")
    if my_profiles:
        parts.append(f"My bids {sum(n for _, n in my_profiles):,}")
    head = f"{who} — {' · '.join(parts)}" if parts else who

    table = _table(kind)
    ordered = sorted(rows, key=lambda r: (bool(r.error), -_row_bids(r), r.bidderName.lower()))
    lines = [_bidder_line(kind, r) for r in ordered]
    if ok:
        lines += [table.rule(), _totals_line(kind, _totals(ok))]
    if my_profiles:
        lines += _my_bids_lines(kind, my_profiles)
    columns = [table.head()] if rows else []  # a My-bids-only owner's table is just the My bids lines

    cont = f"{who} (cont.)\n"
    bodies: list[str] = []
    chunk: list[str] = []
    for line in lines:
        prefix = cont if bodies else ""
        if chunk and len(prefix + _code(columns + chunk + [line])) > MAX_SECTION_CHARS:
            bodies.append(prefix + _code(columns + chunk))
            chunk = []
        chunk.append(line)
    if chunk:
        bodies.append((cont if bodies else "") + _code(columns + chunk))
    return head, bodies


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
    Only bidders with bids are listed. `my_bids` maps owner id -> {profile name: own bids}; it adds lines to the
    owner's table and is never counted in a total. `updated_at` is set when a posted report is edited after
    a review, and shown in the footer."""
    start, end = start.astimezone(tz), end.astimezone(tz)
    period_key = rows[0].periodKey if rows else end.date().isoformat()  # the report day the buttons open
    listed = [r for r in rows if not _is_empty(r)]
    ok = [r for r in listed if not r.error]
    money = fmt_usd(round(sum(r.amount or 0 for r in ok), 2))
    link = f"<{app_url}/bidders|View in app>"
    buttons = app_url.startswith("http")  # Slack refuses a link button without a usable URL
    my_profiles = {str(k): shown for k, v in (my_bids or {}).items() if (shown := _my_profiles(v))}

    by_owner: dict[str, list[BidReportRow]] = defaultdict(list)
    for r in listed:
        by_owner[str(r.ownerId)].append(r)
    for k in my_profiles:
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

    fallback += _stats(ok) + (f" · {money}" if kind == "weekly" else "")
    summary = _totals_text(_totals(ok), bidders=len(ok)) + (f" · {money}" if kind == "weekly" else "")
    footer = f"*Total: {summary}* · {link}"
    if updated_at is not None:
        updated_at = updated_at.astimezone(tz)
        footer += f" · updated {updated_at:%a} {_time(updated_at)}"

    # Packed in units that must not be split across messages: an owner's header and its first body stay together.
    units: list[list[dict]] = [[_section(header)]]
    for owner_id, owner, owner_rows in resolved:
        accessory = None
        if buttons:
            pending = sum(r.pending or 0 for r in owner_rows if not r.error)
            accessory = _owner_button(kind, app_url, owner_id, period_key, pending)
        head, bodies = _owner_sections(kind, owner.label, owner_rows, my_profiles.get(owner_id, ()))
        units.append([_section(head, accessory)] + [_section(b) for b in bodies[:1]])
        units.extend([_section(b)] for b in bodies[1:])
    units.append([_section(footer)])
    messages: list[list[dict]] = [[]]
    for unit in units:
        if messages[-1] and len(messages[-1]) + len(unit) > MAX_BLOCKS:
            messages.append([])
        messages[-1].extend(unit)
    return fallback, messages
