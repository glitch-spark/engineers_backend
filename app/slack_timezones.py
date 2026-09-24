"""Fixed Slack digest timezones: one IANA zone per UTC hour from −12 to +14.

Avoids full Intl zone lists (many names fail older tzdata / ZoneInfo).
Dropdown labels use the zone's current GMT offset, so daylight time is shown.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# Ensure IANA DB is available on slim images (Railway) without system zoneinfo.
try:
    import tzdata  # noqa: F401
except ImportError:
    pass

# (iana_id, place name) — 27 entries, UTC−12 … UTC+14 inclusive.
# Prefer widely-shipped zone names so Railway/older tzdata accept them.
# GMT offsets are not stored here; they follow daylight time at display time.
SLACK_DIGEST_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("Etc/GMT+12", "UTC"),
    ("Pacific/Pago_Pago", "Pacific/Pago Pago"),
    ("Pacific/Honolulu", "Pacific/Honolulu"),
    ("America/Anchorage", "America/Anchorage"),
    ("America/Los_Angeles", "America/Los Angeles"),
    ("America/Denver", "America/Denver"),
    ("America/Chicago", "America/Chicago"),
    ("America/New_York", "America/New York"),
    ("America/Halifax", "America/Halifax"),
    ("America/Sao_Paulo", "America/Sao Paulo"),
    ("America/Noronha", "America/Noronha"),
    ("Atlantic/Azores", "Atlantic/Azores"),
    ("UTC", "UTC"),
    ("Europe/Paris", "Europe/Paris"),
    ("Europe/Athens", "Europe/Athens"),
    ("Europe/Moscow", "Europe/Moscow"),
    ("Asia/Dubai", "Asia/Dubai"),
    ("Asia/Karachi", "Asia/Karachi"),
    ("Asia/Dhaka", "Asia/Dhaka"),
    ("Asia/Bangkok", "Asia/Bangkok"),
    ("Asia/Hong_Kong", "Asia/Hong Kong"),
    ("Asia/Tokyo", "Asia/Tokyo"),
    ("Australia/Brisbane", "Australia/Brisbane"),
    ("Pacific/Noumea", "Pacific/Noumea"),
    ("Pacific/Auckland", "Pacific/Auckland"),
    ("Pacific/Tongatapu", "Pacific/Tongatapu"),
    ("Pacific/Kiritimati", "Pacific/Kiritimati"),
)

SLACK_DIGEST_TIMEZONE_IDS: frozenset[str] = frozenset(z for z, _ in SLACK_DIGEST_TIMEZONES)

_DEFAULT = "America/New_York"


def normalize_slack_timezone(name: Optional[str]) -> str:
    """Return an allowlisted IANA id, or America/New_York."""
    cleaned = (name or "").strip()
    if cleaned in SLACK_DIGEST_TIMEZONE_IDS:
        return cleaned
    return _DEFAULT


def validate_timezone(name: str) -> str:
    """Accept only the fixed digest list."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("timezone is required")
    if cleaned in SLACK_DIGEST_TIMEZONE_IDS:
        # Confirm ZoneInfo can load it on this host.
        try:
            ZoneInfo(cleaned)
        except Exception as exc:
            raise ValueError(f"invalid timezone: {cleaned}") from exc
        return cleaned
    raise ValueError(
        f"invalid timezone: {cleaned}. Choose one of the standard GMT offsets."
    )


def zoneinfo_for_slack(name: Optional[str]) -> ZoneInfo:
    return ZoneInfo(normalize_slack_timezone(name))


def format_gmt_offset(delta: timedelta) -> str:
    """Current offset as GMT−4:00 / GMT+5:30. Uses a Unicode minus."""
    total = int(delta.total_seconds())
    sign = "+" if total >= 0 else "−"
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"GMT{sign}{hours}:{minutes:02d}"


def timezone_label(iana: str, place: str, *, when: Optional[datetime] = None) -> str:
    zone = ZoneInfo(iana)
    moment = when.astimezone(zone) if when is not None else datetime.now(zone)
    gmt = format_gmt_offset(moment.utcoffset() or timedelta(0))
    if iana == "Etc/GMT+12":
        return gmt.replace("GMT", "UTC", 1)
    return f"{place} ({gmt})"


def slack_timezone_catalog(*, when: Optional[datetime] = None) -> list[dict[str, str]]:
    return [
        {"value": z, "label": timezone_label(z, place, when=when)}
        for z, place in SLACK_DIGEST_TIMEZONES
    ]
