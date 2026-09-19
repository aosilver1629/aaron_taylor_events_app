"""Everything is stored in UTC; every user-facing render is Pacific."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")
PACIFIC = ZoneInfo("America/Los_Angeles")


def to_pacific(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(PACIFIC)


def format_event_time(dt: datetime) -> str:
    """e.g. 'Sat 8/29 7pm', matching the SMS format in the spec."""
    local = to_pacific(dt)
    day = local.strftime("%a")
    date = f"{local.month}/{local.day}"
    hour12 = local.strftime("%I").lstrip("0") or "12"
    minute = local.minute
    ampm = local.strftime("%p").lower()
    time_str = f"{hour12}{ampm}" if minute == 0 else f"{hour12}:{minute:02d}{ampm}"
    return f"{day} {date} {time_str}"


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_iso_datetime(value) -> datetime | None:
    """Parses an ISO-8601 string (how every candidate/preview event's
    start_at/end_at arrives, whether from Ticketmaster, Claude extraction,
    or a client re-POSTing a previewed event), tolerating a trailing 'Z'.
    Returns None for anything falsy or unparseable rather than raising.
    """
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None
