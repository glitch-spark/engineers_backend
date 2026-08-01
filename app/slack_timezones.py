"""Fixed Slack digest timezones: one IANA zone per UTC hour from −12 to +14.

Avoids full Intl zone lists (many names fail older tzdata / ZoneInfo).
"""

from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo

# (iana_id, label) — 27 entries, UTC−12 … UTC+14 inclusive.
# Prefer widely-shipped zone names so Railway/older tzdata accept them.
SLACK_DIGEST_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("Etc/GMT+12", "UTC−12:00"),
    ("Pacific/Pago_Pago", "Pacific/Pago Pago (GMT−11:00)"),
    ("Pacific/Honolulu", "Pacific/Honolulu (GMT−10:00)"),
    ("America/Anchorage", "America/Anchorage (GMT−9:00)"),
    ("America/Los_Angeles", "America/Los Angeles (GMT−8:00)"),
    ("America/Denver", "America/Denver (GMT−7:00)"),
    ("America/Chicago", "America/Chicago (GMT−6:00)"),
    ("America/New_York", "America/New York (GMT−5:00)"),
    ("America/Halifax", "America/Halifax (GMT−4:00)"),
    ("America/Sao_Paulo", "America/Sao Paulo (GMT−3:00)"),
    ("America/Noronha", "America/Noronha (GMT−2:00)"),
    ("Atlantic/Azores", "Atlantic/Azores (GMT−1:00)"),
    ("UTC", "UTC (GMT+0:00)"),
    ("Europe/Paris", "Europe/Paris (GMT+1:00)"),
    ("Europe/Athens", "Europe/Athens (GMT+2:00)"),
    ("Europe/Moscow", "Europe/Moscow (GMT+3:00)"),
    ("Asia/Dubai", "Asia/Dubai (GMT+4:00)"),
    ("Asia/Karachi", "Asia/Karachi (GMT+5:00)"),
    ("Asia/Dhaka", "Asia/Dhaka (GMT+6:00)"),
    ("Asia/Bangkok", "Asia/Bangkok (GMT+7:00)"),
    ("Asia/Hong_Kong", "Asia/Hong Kong (GMT+8:00)"),
    ("Asia/Tokyo", "Asia/Tokyo (GMT+9:00)"),
    ("Australia/Brisbane", "Australia/Brisbane (GMT+10:00)"),
    ("Pacific/Noumea", "Pacific/Noumea (GMT+11:00)"),
    ("Pacific/Auckland", "Pacific/Auckland (GMT+12:00)"),
    ("Pacific/Tongatapu", "Pacific/Tongatapu (GMT+13:00)"),
    ("Pacific/Kiritimati", "Pacific/Kiritimati (GMT+14:00)"),
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


def slack_timezone_catalog() -> list[dict[str, str]]:
    return [{"value": z, "label": label} for z, label in SLACK_DIGEST_TIMEZONES]
